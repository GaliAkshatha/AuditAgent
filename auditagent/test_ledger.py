"""
AuditAgent — Test Ledger & Rollback
Records every mutating request (POST/PATCH/PUT/DELETE) made during an
*active* test run against a target app, and can roll them all back
afterward — so testing a real, demo-account-backed app doesn't leave
garbage data behind (or worse, leave real state damaged).

Compensating-action rules, by HTTP method:
    POST (create)   -> DELETE the created resource (needs its id/location
                        from the response body)
    PATCH/PUT (update) -> PATCH/PUT back to the pre-mutation state (needs
                        a GET captured BEFORE the mutation happened)
    DELETE          -> POST the pre-deletion state back to the collection
                        endpoint (needs a GET captured BEFORE the delete)

Rollback runs compensating actions in REVERSE order (last action undone
first) — the same principle as an undo stack, since a later action may
depend on an earlier one still existing.

Honest limitations, worth internalizing before relying on this:
    - Recreating a deleted resource gets a NEW id — anything that
      referenced the old id (foreign keys, other records) will NOT be
      automatically repaired. This is a fundamental limitation of undoing
      a delete on relational data, not a bug here.
    - Side effects with no data representation (an email sent, a webhook
      fired, a payment charged, a cache invalidated elsewhere) cannot be
      undone by any ledger — rollback only reverses the data you can see
      through the API itself.
    - A POST whose response doesn't include an id/location for the
      created resource can't be compensated automatically — it's
      reported as non-reversible rather than silently skipped.
    - Rollback can itself fail partway through (network issue, the
      resource was already modified by something else) — the rollback
      report surfaces per-action success/failure rather than assuming
      success.

Because of all this: active testing should be opt-in, scoped to verified
domains, and — as the person building this rightly suggested — pointed at
a demo/test account, never production data. Rollback is a safety net on
top of that, not a replacement for it.
"""

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

import networkx as nx


@dataclass
class ActionRecord:
    method: str
    url: str
    request_body: dict | None
    response_status: int
    response_body: dict | None
    pre_state: dict | None = None  # GET result captured BEFORE a PATCH/PUT/DELETE
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class TestLedger:
    def __init__(self):
        self.actions: list[ActionRecord] = []

    def record(
        self,
        method: str,
        url: str,
        request_body: dict | None,
        response_status: int,
        response_body: dict | None,
        pre_state: dict | None = None,
    ) -> ActionRecord:
        record = ActionRecord(
            method=method.upper(), url=url, request_body=request_body,
            response_status=response_status, response_body=response_body, pre_state=pre_state,
        )
        self.actions.append(record)
        return record

    def to_dict(self) -> dict:
        return {"actions": [asdict(a) for a in self.actions]}

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)

    @classmethod
    def load(cls, path: str) -> "TestLedger":
        with open(path) as f:
            data = json.load(f)
        ledger = cls()
        ledger.actions = [ActionRecord(**a) for a in data["actions"]]
        return ledger


def _extract_resource_id(response_body: dict | None) -> str | None:
    """Best-effort search for a created resource's identifier in a POST
    response — covers the common field names without guessing wildly."""
    if not isinstance(response_body, dict):
        return None
    for key in ("id", "_id", "uuid", "ID", "Id"):
        if key in response_body:
            return str(response_body[key])
    return None


def _flatten_values(obj):
    """Yields every scalar value in a nested dict/list structure — used to
    scan request bodies for resource-id references without assuming any
    specific field-naming convention (post_id, postId, parent, etc. all
    just show up as values somewhere in the structure)."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_values(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten_values(v)
    else:
        yield obj


def build_dependency_graph(ledger: TestLedger) -> nx.DiGraph:
    """Builds a graph of ACTIONS (nodes = ledger indices), where edge i->j
    means "action j depends on a resource action i created" — detected
    two ways: j's URL path references an id i created (e.g. PATCH
    /items/{id}), or j's request body contains a value matching an id i
    created (e.g. a comment's {"post_id": 1} referencing post 1).

    Deliberately determines "who created this id" using each action's
    recorded TIMESTAMP, not its position in ledger.actions. A first version
    of this used a single forward pass over the list, implicitly assuming
    list order matches true chronological order — which defeats the whole
    point of this function, since the entire reason to build a dependency
    graph instead of just reversing the list is to stay correct when
    those two orders DON'T match (merged logs, reconstructed history).
    Caught this by actually testing with a deliberately shuffled ledger:
    the list-order version silently found zero edges for a real
    dependency, because the true creator appeared after its dependent in
    list position — exactly the case this was supposed to handle.

    This graph is a DAG as long as timestamps are accurate (an action
    cannot truly depend on something that hasn't happened yet) — rollback()
    still guards against a cycle via NetworkX's exception rather than
    assuming that invariant always holds against messy real-world data."""
    G = nx.DiGraph()

    # Determine creation order from timestamps, not list position.
    timestamp_order = sorted(range(len(ledger.actions)), key=lambda i: ledger.actions[i].timestamp)

    created_by: dict[str, int] = {}
    for i in timestamp_order:
        record = ledger.actions[i]
        if record.method == "POST" and 200 <= record.response_status < 300:
            resource_id = _extract_resource_id(record.response_body)
            if resource_id and resource_id not in created_by:
                created_by[resource_id] = i

    for i, record in enumerate(ledger.actions):
        G.add_node(i, method=record.method, url=record.url)

        url_tail = record.url.rstrip("/").rsplit("/", 1)[-1]
        if url_tail in created_by and created_by[url_tail] != i:
            G.add_edge(created_by[url_tail], i)

        if record.request_body:
            for value in _flatten_values(record.request_body):
                value_str = str(value)
                if value_str in created_by and created_by[value_str] != i:
                    G.add_edge(created_by[value_str], i)

    return G


def compute_rollback_order(ledger: TestLedger) -> list[int]:
    """Returns ledger indices in the order compensations should run:
    dependents undone before their dependencies, via topological sort of
    the reversed dependency graph. Ties (actions with no ordering
    constraint between them) break toward higher index first — matches
    the intuitive "undo more recent stuff first" default when the graph
    doesn't force a specific order.

    For a normal, strictly time-ordered ledger this produces the exact
    same order as reversing the list (verified by test — this is not a
    behavior change for the common case). Falls back to simple reverse
    order if the graph somehow contains a cycle (shouldn't happen against
    real timestamped data, but real-world data is messy — fail safe
    rather than raising)."""
    G = build_dependency_graph(ledger)
    reversed_graph = G.reverse(copy=True)
    try:
        return list(nx.lexicographical_topological_sort(reversed_graph, key=lambda n: -n))
    except nx.NetworkXUnfeasible:
        return list(range(len(ledger.actions) - 1, -1, -1))


def compute_compensating_action(record: ActionRecord) -> dict | None:
    """Returns {'method', 'url', 'body'} describing how to undo this one
    action, or None if it can't be automatically compensated (a real,
    honest outcome — not every action is reversible, see module docstring)."""
    if record.method == "POST" and 200 <= record.response_status < 300:
        resource_id = _extract_resource_id(record.response_body)
        if not resource_id:
            return None  # can't locate what was created — not reversible
        return {"method": "DELETE", "url": f"{record.url.rstrip('/')}/{resource_id}", "body": None}

    if record.method in ("PATCH", "PUT") and record.pre_state is not None:
        return {"method": record.method, "url": record.url, "body": record.pre_state}

    if record.method == "DELETE" and record.pre_state is not None:
        collection_url = record.url.rsplit("/", 1)[0]
        return {"method": "POST", "url": collection_url, "body": record.pre_state}

    return None


def rollback(ledger: TestLedger, session=None, dry_run: bool = True) -> dict:
    """Runs compensating actions for every recorded mutation, in reverse
    order. dry_run=True (default) computes what WOULD run without making
    any requests — always review this before running for real.

    `session` is anything with a `.request(method, url, json=...)` method
    (e.g. a `requests.Session()`) — not imported directly here so this
    module has zero hard dependency on a specific HTTP client for the
    dry-run path, which is also what makes it trivially unit-testable.

    Self-canceling pairs: if a resource was CREATED and later DELETED
    within the same ledger (a full create/delete cycle in one test run),
    naively compensating both independently is actually wrong — recreating
    the deleted resource gets a NEW id, which makes the original POST's
    "DELETE this id" compensation stale by the time it runs, and it fails.
    Found this by actually running a rollback, not by reasoning about it
    in the abstract. Detected here and both actions are skipped as a
    matched pair instead — the net effect on that resource was already
    zero, so there's nothing to roll back."""
    self_canceling_ids = _find_self_canceling_pairs(ledger)
    rollback_order = compute_rollback_order(ledger)
    results = []

    for original_index in rollback_order:
        record = ledger.actions[original_index]

        if original_index in self_canceling_ids:
            results.append({
                "original_method": record.method, "original_url": record.url,
                "compensable": True, "self_canceling": True, "executed": False,
            })
            continue

        comp = compute_compensating_action(record)

        if comp is None:
            results.append({
                "original_method": record.method, "original_url": record.url,
                "compensable": False,
            })
            continue

        entry = {
            "original_method": record.method, "original_url": record.url,
            "compensable": True, "compensating_action": comp,
        }

        if dry_run:
            entry["executed"] = False
            results.append(entry)
            continue

        if session is None:
            entry["executed"] = False
            entry["error"] = "No session provided for a non-dry-run rollback"
            results.append(entry)
            continue

        try:
            resp = session.request(comp["method"], comp["url"], json=comp["body"], timeout=10)
            entry["executed"] = True
            entry["success"] = resp.status_code < 400
            entry["status_code"] = resp.status_code
        except Exception as e:
            entry["executed"] = True
            entry["success"] = False
            entry["error"] = str(e)

        results.append(entry)

    compensable_count = sum(1 for r in results if r["compensable"])
    failed_count = sum(1 for r in results if r.get("executed") and not r.get("success", True))

    return {
        "total_actions": len(ledger.actions),
        "compensable_actions": compensable_count,
        "non_compensable_actions": len(ledger.actions) - compensable_count,
        "self_canceling_pairs": len(self_canceling_ids) // 2,
        "dry_run": dry_run,
        "failed_rollbacks": failed_count,
        "results": results,
    }


def _find_self_canceling_pairs(ledger: TestLedger) -> set[int]:
    """Finds (POST index, DELETE index) pairs where the DELETE removes
    exactly the resource the POST created — these need to be skipped as a
    matched pair, not compensated independently (see rollback()'s
    docstring for why).

    Also pulls in any OTHER action (e.g. a PATCH) that operated on that
    same resource between its creation and deletion. Found this by
    actually executing a rollback: a POST -> PATCH -> DELETE sequence on
    one resource correctly skipped the POST/DELETE pair, but the PATCH in
    the middle was still treated as independently compensable — its
    compensation tried to PATCH a resource that, per the pair decision,
    we're treating as having never existed, and got a real 404. Once a
    resource's whole lifecycle (create through delete) is being skipped,
    everything that happened to it in between has nothing left to
    compensate against either."""
    created_at: dict[str, int] = {}
    canceling: set[int] = set()

    for i, record in enumerate(ledger.actions):
        if record.method == "POST" and 200 <= record.response_status < 300:
            resource_id = _extract_resource_id(record.response_body)
            if resource_id:
                created_at[resource_id] = i

        elif record.method == "DELETE":
            url_tail = record.url.rstrip("/").rsplit("/", 1)[-1]
            if url_tail in created_at:
                create_idx = created_at[url_tail]
                canceling.add(create_idx)
                canceling.add(i)
                for k, other_record in enumerate(ledger.actions):
                    if k in (create_idx, i):
                        continue
                    other_tail = other_record.url.rstrip("/").rsplit("/", 1)[-1]
                    if other_tail == url_tail:
                        canceling.add(k)

    return canceling
