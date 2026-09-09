"""
AuditAgent v1.5 — Static Analysis Agent
Clones a GitHub repo and extracts every route/endpoint the source code
defines, regardless of whether the live crawler ever reached it. This is
what makes "every endpoint" actually true — a crawler only sees what's
linked from the UI; this reads the routing code directly.

Framework detection + route extraction here is regex/heuristic-based, not a
full AST parse. That's a deliberate v1.5 scope cut: it covers the common,
idiomatic route-definition patterns for each framework below, but won't
catch every possible way a route could be registered (e.g. routes built
dynamically in a loop, or heavily macro'd Django URL includes). Good enough
to find the vast majority of real endpoints; a proper AST-based parser per
language is a natural upgrade once this proves the concept out.

Supported frameworks (detected automatically from repo structure):
    - Express / Node.js  (app.get/post/put/delete/patch, router.*)
    - Flask               (@app.route, @app.get/post/etc.)
    - FastAPI              (@app.get/post/put/delete/patch, APIRouter)
    - Django                (path()/re_path() in urls.py)
    - Next.js (App Router)  (app/**/route.js|ts — file-based API routes)
    - Next.js (Pages Router) (pages/api/**/*.js|ts — file-based API routes)

Usage:
    python static_analysis.py https://github.com/user/repo
    python static_analysis.py /local/path/to/repo
    python static_analysis.py https://github.com/user/repo --output endpoints.json
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict


@dataclass
class Endpoint:
    method: str
    path: str
    file: str
    line: int
    framework: str


@dataclass
class StaticAnalysisReport:
    repo: str
    detected_frameworks: list[str] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    files_scanned: int = 0
    errors: list[str] = field(default_factory=list)


# Directories we never want to walk into — huge, irrelevant, or third-party.
# Test directories are included because test files legitimately contain
# route-like decorator patterns (e.g. Flask's own test_json.py defines
# @app.route('/json') fixtures) that aren't real app endpoints — scanning
# them produces noise, not findings.
SKIP_DIRS = {
    "node_modules", ".git", "venv", ".venv", "__pycache__", "dist", "build",
    ".next", "vendor", "target", "coverage", ".pytest_cache", "site-packages",
    "tests", "test", "__tests__", "spec", "specs", "examples", "docs",
}

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}


def clone_repo(repo_url_or_path: str) -> tuple[str, bool]:
    """Returns (local_path, is_temp). Clones if given a URL, uses the path
    directly if it's already local."""
    if os.path.isdir(repo_url_or_path):
        return repo_url_or_path, False

    tmp_dir = tempfile.mkdtemp(prefix="auditagent_repo_")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url_or_path, tmp_dir],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError("git clone timed out after 120s")

    return tmp_dir, True


def walk_files(root: str, extensions: tuple[str, ...]) -> list[str]:
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            if fname.endswith(extensions):
                matches.append(os.path.join(dirpath, fname))
    return matches


def detect_frameworks(root: str) -> list[str]:
    detected = []

    pkg_json = os.path.join(root, "package.json")
    if os.path.isfile(pkg_json):
        try:
            with open(pkg_json) as f:
                pkg = json.load(f)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "express" in deps:
                detected.append("express")
            if "next" in deps:
                detected.append("nextjs")
        except Exception:
            pass

    for req_file in ("requirements.txt", "Pipfile", "pyproject.toml"):
        path = os.path.join(root, req_file)
        if os.path.isfile(path):
            try:
                content = open(path).read().lower()
                if "flask" in content:
                    detected.append("flask")
                if "fastapi" in content:
                    detected.append("fastapi")
                if "django" in content:
                    detected.append("django")
            except Exception:
                pass

    # Manifest-only detection misses real-world cases: requirements.txt can
    # point at a nested file (e.g. "-r requirements/base.txt") instead of
    # listing dependencies directly, or a repo might have no manifest at
    # all. Fall back to scanning actual import statements in source files —
    # this is what the app *actually does*, not what a manifest claims.
    if not any(d in detected for d in ("flask", "fastapi", "django")):
        detected.extend(_detect_python_frameworks_by_import(root))
    if "express" not in detected:
        if _detect_express_by_import(root):
            detected.append("express")

    # Fallback: scan for telltale files even without a manifest match
    # (covers repos with unusual/missing dependency files).
    if "django" not in detected:
        if any(f.endswith("manage.py") for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))):
            detected.append("django")

    return list(dict.fromkeys(detected))  # dedupe, preserve order


PYTHON_IMPORT_PATTERNS = {
    "flask": re.compile(r'^\s*(?:from|import)\s+flask\b', re.IGNORECASE | re.MULTILINE),
    "fastapi": re.compile(r'^\s*(?:from|import)\s+fastapi\b', re.IGNORECASE | re.MULTILINE),
    "django": re.compile(r'^\s*(?:from|import)\s+django\b', re.IGNORECASE | re.MULTILINE),
}


def _detect_python_frameworks_by_import(root: str, max_files: int = 200) -> list[str]:
    found = set()
    for filepath in walk_files(root, (".py",))[:max_files]:
        try:
            content = open(filepath, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for fw, pattern in PYTHON_IMPORT_PATTERNS.items():
            if fw not in found and pattern.search(content):
                found.add(fw)
        if len(found) == len(PYTHON_IMPORT_PATTERNS):
            break
    return list(found)


EXPRESS_IMPORT_RE = re.compile(r'''(?:require\(['"]express['"]\)|from\s+['"]express['"])''')


def _detect_express_by_import(root: str, max_files: int = 200) -> bool:
    for filepath in walk_files(root, (".js", ".ts", ".mjs", ".cjs"))[:max_files]:
        try:
            content = open(filepath, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if EXPRESS_IMPORT_RE.search(content):
            return True
    return False


# ---- Express -----------------------------------------------------------

EXPRESS_ROUTE_RE = re.compile(
    r'''\b(?:app|router)\s*\.\s*(get|post|put|delete|patch|options|head)\s*\(\s*['"`]([^'"`]+)['"`]''',
    re.IGNORECASE,
)


def extract_express_routes(root: str) -> list[Endpoint]:
    endpoints = []
    for filepath in walk_files(root, (".js", ".ts", ".mjs", ".cjs")):
        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        for i, line in enumerate(lines, start=1):
            for m in EXPRESS_ROUTE_RE.finditer(line):
                method, path = m.group(1).upper(), m.group(2)
                endpoints.append(Endpoint(method, path, filepath, i, "express"))

    prefixes = resolve_express_mount_prefixes(root)
    for ep in endpoints:
        prefix = prefixes.get(ep.file)
        if prefix:
            ep.path = _join_paths(prefix, ep.path)

    return endpoints


IMPORT_RE = re.compile(
    r'''(?:'''
    r'''import\s+(\w+)\s*,\s*\{[^}]*\}\s*from\s+['"]([^'"]+)['"]'''   # import X, { Y } from '...'  (default + named)
    r'''|import\s+(\w+)\s+from\s+['"]([^'"]+)['"]'''                  # import X from '...'
    r'''|const\s+(\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)'''     # const X = require('...')
    r''')'''
)
MOUNT_RE = re.compile(
    r'''\b(?:app|router)\s*\.\s*use\s*\(\s*['"]([^'"]+)['"]\s*,\s*(?:\w+\s*,\s*)*(\w+)\s*\)'''
)


def resolve_express_mount_prefixes(root: str) -> dict[str, str]:
    """Resolves the FULL mount-prefix chain for every Express router file —
    not just one level. Real apps commonly nest routers through an
    aggregator (e.g. app.ts mounts routes/index.ts at '/api', which itself
    mounts auth.routes.ts at '/auth' — the real live path is
    '/api/auth/login', not just '/auth/login'). This walks that whole
    chain recursively, since a single-level lookup silently produces the
    wrong path for anything mounted through an aggregator file.

    Also handles the combined `import X, { Y } from '...'` syntax (default
    export + a named export together) — a plain `import X from '...'`
    pattern alone misses this form and silently drops those routers from
    resolution entirely. This was caught by testing against a real repo:
    two routers using this exact syntax were invisible to the resolver
    and ended up colliding on the same bare path during the graph merge.

    Still heuristic, not a full module resolver: matches relative imports
    only, resolves via suffix-matching against real files on disk (also
    matching index files, e.g. './routes' -> routes/index.ts), and breaks
    ties on ambiguous multi-mount routers by taking the first match.
    """
    all_files = walk_files(root, (".js", ".ts", ".mjs", ".cjs"))

    # mount_edges[target_file] = (source_file, prefix) — "target_file's
    # routes are mounted at `prefix` relative to source_file."
    mount_edges: dict[str, tuple[str, str]] = {}

    for filepath in all_files:
        try:
            content = open(filepath, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        if ".use(" not in content:
            continue

        var_to_import_path: dict[str, str] = {}
        for m in IMPORT_RE.finditer(content):
            groups = m.groups()
            # groups come in (var, path) pairs across the alternatives above
            for i in range(0, len(groups), 2):
                var, imp_path = groups[i], groups[i + 1]
                if var and imp_path:
                    var_to_import_path[var] = imp_path

        for m in MOUNT_RE.finditer(content):
            prefix, var = m.group(1), m.group(2)
            imp_path = var_to_import_path.get(var)
            if not imp_path or not imp_path.startswith("."):
                continue  # only resolve local/relative imports, skip package imports

            target_dir = os.path.dirname(filepath)
            candidate = os.path.normpath(os.path.join(target_dir, imp_path))
            for real_file in all_files:
                real_no_ext = os.path.splitext(real_file)[0]
                real_no_index = re.sub(r"[/\\]index$", "", real_no_ext)
                if real_no_ext == candidate or real_no_ext.endswith(candidate) or real_no_index.endswith(candidate):
                    if real_file not in mount_edges:  # first match wins on ambiguity
                        mount_edges[real_file] = (filepath, prefix)
                    break

    memo: dict[str, str] = {}

    def resolve_full_prefix(file: str, seen: frozenset) -> str:
        if file in memo:
            return memo[file]
        if file not in mount_edges or file in seen:
            return ""  # root/entrypoint, or a cycle guard
        source_file, prefix = mount_edges[file]
        parent_prefix = resolve_full_prefix(source_file, seen | {file})
        full = _join_paths(parent_prefix, prefix) if parent_prefix else prefix
        memo[file] = full
        return full

    return {f: resolve_full_prefix(f, frozenset()) for f in mount_edges}


def _join_paths(prefix: str, path: str) -> str:
    prefix = prefix.rstrip("/")
    path = "/" + path.lstrip("/")
    if path == "/":
        return prefix or "/"
    return prefix + path


# ---- Flask ---------------------------------------------------------------

FLASK_ROUTE_RE = re.compile(
    r'''@\w+\.route\s*\(\s*['"`]([^'"`]+)['"`](?:.*?methods\s*=\s*\[([^\]]*)\])?''',
    re.IGNORECASE | re.DOTALL,
)
FLASK_SHORTCUT_RE = re.compile(
    r'''@\w+\.(get|post|put|delete|patch)\s*\(\s*['"`]([^'"`]+)['"`]''',
    re.IGNORECASE,
)


def extract_flask_routes(root: str) -> list[Endpoint]:
    endpoints = []
    for filepath in walk_files(root, (".py",)):
        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            continue
        lines = content.splitlines()

        for i, line in enumerate(lines, start=1):
            m = FLASK_ROUTE_RE.search(line)
            if m:
                path = m.group(1)
                methods_str = m.group(2)
                methods = ["GET"]
                if methods_str:
                    methods = [mm.strip(" '\"").upper() for mm in methods_str.split(",") if mm.strip()]
                for method in methods:
                    endpoints.append(Endpoint(method, path, filepath, i, "flask"))
                continue
            m2 = FLASK_SHORTCUT_RE.search(line)
            if m2:
                endpoints.append(Endpoint(m2.group(1).upper(), m2.group(2), filepath, i, "flask"))
    return endpoints


# ---- FastAPI ---------------------------------------------------------------

FASTAPI_ROUTE_RE = re.compile(
    r'''@\w+\.(get|post|put|delete|patch|options|head)\s*\(\s*['"`]([^'"`]+)['"`]''',
    re.IGNORECASE,
)


def extract_fastapi_routes(root: str) -> list[Endpoint]:
    endpoints = []
    for filepath in walk_files(root, (".py",)):
        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        for i, line in enumerate(lines, start=1):
            m = FASTAPI_ROUTE_RE.search(line)
            if m:
                endpoints.append(Endpoint(m.group(1).upper(), m.group(2), filepath, i, "fastapi"))
    return endpoints


# ---- Django ---------------------------------------------------------------

DJANGO_PATH_RE = re.compile(
    r'''\b(?:path|re_path)\s*\(\s*r?['"`]([^'"`]*)['"`]''',
)


def extract_django_routes(root: str) -> list[Endpoint]:
    endpoints = []
    for filepath in walk_files(root, ("urls.py",)):
        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
        for i, line in enumerate(lines, start=1):
            m = DJANGO_PATH_RE.search(line)
            if m:
                path = "/" + m.group(1).lstrip("/")
                # Django url patterns don't map to a single HTTP method —
                # the view function decides. We record it as ANY.
                endpoints.append(Endpoint("ANY", path, filepath, i, "django"))
    return endpoints


# ---- Next.js (file-based routing) ------------------------------------------

def extract_nextjs_routes(root: str) -> list[Endpoint]:
    endpoints = []

    # App Router: app/**/route.{js,ts} — path derived from folder structure.
    for filepath in walk_files(root, ("route.js", "route.ts")):
        rel = os.path.relpath(filepath, root)
        if not rel.startswith(("app" + os.sep, "src" + os.sep + "app" + os.sep)):
            continue
        route_path = _nextjs_path_from_file(rel, anchor="app")
        try:
            content = open(filepath, encoding="utf-8", errors="ignore").read()
        except Exception:
            content = ""
        methods_found = set(re.findall(r'\bexport\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH)\b', content))
        if not methods_found:
            methods_found = {"GET"}
        for method in methods_found:
            endpoints.append(Endpoint(method, route_path, filepath, 1, "nextjs-app-router"))

    # Pages Router: pages/api/**/*.{js,ts} — every file is one endpoint,
    # method is decided inside the handler, so we record it as ANY.
    for filepath in walk_files(root, (".js", ".ts")):
        rel = os.path.relpath(filepath, root)
        if os.sep + "pages" + os.sep + "api" + os.sep not in (os.sep + rel):
            continue
        route_path = _nextjs_path_from_file(rel, anchor="api")
        endpoints.append(Endpoint("ANY", route_path, filepath, 1, "nextjs-pages-router"))

    return endpoints


def _nextjs_path_from_file(rel_path: str, anchor: str) -> str:
    """Convert a Next.js file path into a URL path.
    e.g. app/api/users/[id]/route.ts -> /api/users/:id
         pages/api/users/[id].ts     -> /api/users/:id
    """
    parts = rel_path.replace("\\", "/").split("/")
    if anchor in parts:
        parts = parts[parts.index(anchor):]
    # drop the filename itself (route.ts / route.js / [id].ts etc.)
    parts = parts[:-1] if parts[-1].startswith("route.") else parts
    if parts and not parts[-1].startswith("route."):
        # pages router: strip extension from the last segment instead
        last = re.sub(r"\.(js|ts)$", "", parts[-1]) if parts else ""
        if last:
            parts[-1] = last
    cleaned = []
    for p in parts:
        if p in ("app", "pages"):
            continue
        p = re.sub(r"^\[(\.\.\.)?([^\]]+)\]$", r":\2", p)  # [id] -> :id
        if p:
            cleaned.append(p)
    return "/" + "/".join(cleaned)


EXTRACTORS = {
    "express": extract_express_routes,
    "flask": extract_flask_routes,
    "fastapi": extract_fastapi_routes,
    "django": extract_django_routes,
    "nextjs": extract_nextjs_routes,
}


def analyze(repo_url_or_path: str) -> StaticAnalysisReport:
    report = StaticAnalysisReport(repo=repo_url_or_path)
    local_path, is_temp = clone_repo(repo_url_or_path)

    try:
        frameworks = detect_frameworks(local_path)
        report.detected_frameworks = frameworks

        if not frameworks:
            report.errors.append(
                "No supported framework detected (looked for Express, Flask, "
                "FastAPI, Django, Next.js). Route extraction skipped."
            )

        for fw in frameworks:
            try:
                report.endpoints.extend(EXTRACTORS[fw](local_path))
            except Exception as e:
                report.errors.append(f"Error extracting {fw} routes: {e}")

        report.files_scanned = sum(1 for _ in walk_files(local_path, (".js", ".ts", ".py", ".mjs", ".cjs")))

    finally:
        if is_temp:
            shutil.rmtree(local_path, ignore_errors=True)

    return report


def print_summary(report: StaticAnalysisReport) -> None:
    print(f"\n{'='*60}")
    print(f"AuditAgent — Static Analysis Report for {report.repo}")
    print(f"{'='*60}")
    print(f"Frameworks detected: {', '.join(report.detected_frameworks) or 'none'}")
    print(f"Files scanned: {report.files_scanned}")
    print(f"Endpoints found: {len(report.endpoints)}\n")

    if report.endpoints:
        by_framework = {}
        for ep in report.endpoints:
            by_framework.setdefault(ep.framework, []).append(ep)
        for fw, eps in by_framework.items():
            print(f"[{fw}] {len(eps)} endpoint(s):")
            for ep in eps[:30]:
                rel_file = ep.file
                print(f"  - {ep.method:<7} {ep.path}   ({rel_file}:{ep.line})")
            if len(eps) > 30:
                print(f"  ... and {len(eps) - 30} more")
            print()

    if report.errors:
        print("⚠ Errors/notes:")
        for e in report.errors:
            print(f"  - {e}")
        print()


def main():
    parser = argparse.ArgumentParser(description="AuditAgent v1.5 — extract every endpoint defined in a repo's source code")
    parser.add_argument("repo", help="GitHub repo URL (https://github.com/user/repo) or local path")
    parser.add_argument("--output", type=str, default=None, help="Write full JSON report to this file")
    args = parser.parse_args()

    report = analyze(args.repo)
    print_summary(report)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(asdict(report), f, indent=2, default=str)
        print(f"Full JSON report written to {args.output}")


if __name__ == "__main__":
    main()
