"""
AuditAgent — Code Reviewer
A pure static, code-only review — no live URL, no crawling, nothing
running. Point it at a GitHub repo and it reads the actual source
(reusing static_analysis.py's clone/file-discovery logic) and asks an
LLM to flag concrete good/bad patterns, short and scannable — developers
reading this want key points, not essays.

This is the FIRST step in the flow: review the code, then separately ask
whether to also audit the deployed version. Keeping this decoupled from
the live-audit pipeline is deliberate — someone might only want the code
review, without ever giving a live URL.
"""

import json
import os
import subprocess
import threading
import time

from static_analysis import clone_repo, walk_files
from llm_provider import chat_completion

# Every review previously re-cloned the repo from scratch, every time,
# for every user — real, measured waste, and something worth fixing
# before this runs on a resource-constrained deployment. `git ls-remote`
# fetches just the remote's current HEAD commit hash — no clone at all —
# so a repeat request for an unchanged repo can skip cloning AND the LLM
# call entirely, not just avoid the clone.
_review_cache: dict[str, dict] = {}
_review_cache_lock = threading.Lock()
CACHE_TTL_S = 3600  # re-check even an unchanged repo at most once/hour


def _get_remote_head_sha(repo_url: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout.split()[0]
    except (subprocess.SubprocessError, OSError):
        pass
    return None

REVIEW_EXTENSIONS = (".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs")
MAX_FILES = 12
MAX_CHARS_PER_FILE = 3000
PRIORITY_NAME_HINTS = (
    "app", "server", "main", "index", "route", "controller", "auth",
    "config", "middleware", "model", "schema",
)


def _select_files(root: str) -> list[str]:
    """Picks a small, high-signal sample rather than everything.

    For a monorepo (apps/web + apps/api + packages/...), naive name-based
    priority alone can let one directory swallow the whole budget —
    tested against a real repo and confirmed this: 10 frontend files got
    picked and zero backend route handlers, missing exactly the files
    most likely to explain a server-side bug. Fixed by reserving budget
    across detected top-level directories, not just sorting by filename."""
    all_files = walk_files(root, REVIEW_EXTENSIONS)

    def name_priority(path: str) -> int:
        name = os.path.basename(path).lower()
        return 0 if any(hint in name for hint in PRIORITY_NAME_HINTS) else 1

    def top_level_dir(path: str) -> str:
        """Groups by the first TWO path segments when the first is a
        generic container (apps/, packages/, services/, libs/) — a plain
        first-segment split put apps/web and apps/api in the SAME group
        ("apps"), which defeated the whole point of round-robining across
        directories. Caught this by re-testing against the real repo
        after the first fix and finding backend files still missing."""
        rel = os.path.relpath(path, root)
        parts = rel.split(os.sep)
        if len(parts) <= 1:
            return "."
        if parts[0] in ("apps", "packages", "services", "libs", "src") and len(parts) > 2:
            return f"{parts[0]}/{parts[1]}"
        return parts[0]

    all_files.sort(key=name_priority)

    # Round-robin across top-level directories so a monorepo's backend
    # doesn't get crowded out by its frontend (or vice versa) — each
    # directory gets a turn before any one directory fills the budget.
    by_dir: dict[str, list[str]] = {}
    for f in all_files:
        by_dir.setdefault(top_level_dir(f), []).append(f)

    selected = []
    while len(selected) < MAX_FILES and any(by_dir.values()):
        for dir_files in by_dir.values():
            if dir_files and len(selected) < MAX_FILES:
                selected.append(dir_files.pop(0))

    return selected


def _read_snippets(root: str, files: list[str]) -> str:
    parts = []
    for filepath in files:
        rel = os.path.relpath(filepath, root)
        try:
            with open(filepath, "r", errors="ignore") as f:
                content = f.read(MAX_CHARS_PER_FILE)
        except OSError:
            continue
        parts.append(f"--- {rel} ---\n{content}")
    return "\n\n".join(parts)


REVIEW_PROMPT = """You are reviewing source code for a developer audience. Be extremely concise — short bullet points only, not paragraphs. Each point should be a single scannable line, 5-15 words.

Review the code below and return ONLY valid JSON, no markdown fences, no preamble, in exactly this shape:
{{
  "good": [{{"title": "short label", "detail": "one short line"}}],
  "bad": [{{"title": "short label", "detail": "one short line", "severity": "high|medium|low"}}]
}}

3-6 items per list. Focus on concrete, real patterns actually visible in this code — input validation, error handling, auth checks, secrets handling, obvious anti-patterns. Do not invent generic advice not grounded in what's actually shown.

CODE:
{code}
"""


def review_code(repo_url: str, provider: str = "gemini", model: str | None = None) -> dict:
    sha = _get_remote_head_sha(repo_url)
    cache_key = repo_url.rstrip("/").lower()
    if sha:
        with _review_cache_lock:
            cached = _review_cache.get(cache_key)
        if cached and cached["sha"] == sha and (time.time() - cached["cached_at"]) < CACHE_TTL_S:
            # Same commit, checked cheaply via ls-remote — skip both the
            # clone AND the LLM call entirely, not just the clone.
            result = dict(cached["result"])
            result["from_cache"] = True
            return result

    local_path, is_temp = clone_repo(repo_url)
    files = []
    try:
        files = _select_files(local_path)
        if not files:
            return {"good": [], "bad": [], "files_reviewed": 0, "error": "No reviewable source files found"}

        code_blob = _read_snippets(local_path, files)
        prompt = REVIEW_PROMPT.format(code=code_blob)

        response = chat_completion(
            messages=[{"role": "user", "content": prompt}],
            provider=provider, model=model,
        )

        cleaned = response.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        result = json.loads(cleaned.strip())
        result["files_reviewed"] = len(files)
        result["file_list"] = [os.path.relpath(f, local_path) for f in files]

        if sha:
            with _review_cache_lock:
                _review_cache[cache_key] = {"sha": sha, "cached_at": time.time(), "result": result}
        return result
    except (json.JSONDecodeError, ValueError) as e:
        return {"good": [], "bad": [], "files_reviewed": len(files),
                "error": f"Model response wasn't valid JSON: {e}"}
    except RuntimeError as e:
        # e.g. missing API key — a real, expected failure mode, not
        # something that should crash the whole endpoint with a 500.
        return {"good": [], "bad": [], "files_reviewed": len(files), "error": str(e)}
    finally:
        if is_temp:
            import shutil
            shutil.rmtree(local_path, ignore_errors=True)
