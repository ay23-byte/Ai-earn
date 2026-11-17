#!/usr/bin/env python3
"""Security checks — secret scan + dependency audit placeholder (stdlib only).

Usage:
  python -m tools.security_check          # scan repo
  python -m tools.security_check --audit  # dependency audit (pip-audit placeholder)
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

_FORB_BP = "BEGIN" + " PRIVATE KEY"
FORBIDDEN = [
    (_FORB_BP, "private key material"),
    (r"sk-[A-Za-z0-9]{20}", "possible OpenAI key"),
    (r"AKIA[0-9A-Z]{16}", "possible AWS key"),
]

def scan_secrets() -> list[str]:
    offenders: list[str] = []
    for p in ROOT.rglob("*.py"):
        s = str(p)
        if ".venv" in s or "__pycache__" in s or ".git" in s:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat, label in FORBIDDEN:
            if re.search(pat, text):
                offenders.append(f"{p}: {label} ({pat})")
    # .env must not exist in repo (gitignored)
    if (ROOT / ".env").exists():
        offenders.append(".env file present in repo — must be gitignored and env-only")
    # Check dashboard never serves sqlite
    for f in [ROOT / "dashboard" / "app.js", ROOT / "dashboard" / "connected.html"]:
        if f.exists() and "0.0.0.0" in f.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(f"{f}: must not bind 0.0.0.0 with local SQLite")
    return offenders


def audit_dependencies() -> int:
    """Truthful dependency audit — stdlib only -> clean; with deps -> pip-audit, fail on vulns."""
    # Determine if project has third-party deps
    has_deps = False
    # Check requirements*.txt for non-comment lines
    for req in ROOT.glob("requirements*.txt"):
        try:
            for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    has_deps = True
                    break
        except Exception:
            continue
    # Check pyproject.toml dependencies via tomllib if available
    pyproj = ROOT / "pyproject.toml"
    if pyproj.exists() and not has_deps:
        try:
            import tomllib  # Python 3.11+
            data = tomllib.loads(pyproj.read_bytes().decode("utf-8", errors="ignore"))
            deps = data.get("project", {}).get("dependencies") or data.get("dependencies") or []
            if isinstance(deps, list) and len(deps) > 0:
                # Filter empty or stdlib-only markers (none in this repo)
                has_deps = any(str(d).strip() for d in deps)
            # Also check optional dependencies
            opt = data.get("project", {}).get("optional-dependencies")
            if isinstance(opt, dict):
                for v in opt.values():
                    if isinstance(v, list) and any(str(x).strip() for x in v):
                        has_deps = True
        except Exception:
            # Fallback: crude text scan for dependencies section with entries
            try:
                txt = pyproj.read_text(encoding="utf-8", errors="ignore")
                # If file contains a dependencies list with quoted entries, consider has_deps
                if "dependencies" in txt and any(c in txt for c in ["\"", "'"]):
                    # Heuristic: if pyproject has dependencies = [] empty, then no deps
                    if "dependencies = []" in txt or 'dependencies = [ ]' in txt:
                        has_deps = False
                    elif "dependencies" in txt:
                        # Check if list is non-empty by looking for a package name pattern
                        if any(pkg in txt.lower() for pkg in ["requests", "flask", "django", "pip-audit", "httpx"]):
                            has_deps = True
            except Exception:
                pass
    if not has_deps:
        print("Dependency audit: stdlib only — no third-party dependencies to audit (clean).")
        print("Policy: stdlib only is clean; if deps added, CI must run pip-audit and fail on known vulns (see docs/SECURITY.md).")
        return 0
    # Deps exist — require real audit
    try:
        result = subprocess.run([sys.executable, "-m", "pip_audit", "--disable-pip"], capture_output=True, text=True, timeout=30)
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        if result.returncode != 0:
            print("Dependency audit FAILED: pip-audit found vulnerabilities (fail per policy).", file=sys.stderr)
        return result.returncode
    except FileNotFoundError:
        print("Dependency audit FAILED: pip-audit not installed but dependencies exist. Install pip-audit and retry (fail per policy).", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("pip-audit timed out", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    if "--audit" in argv:
        return audit_dependencies()
    offenders = scan_secrets()
    if offenders:
        for o in offenders:
            print(f"FAIL: {o}", file=sys.stderr)
        return 1
    print("Security scan passed: no forbidden secret patterns, no 0.0.0.0 DB exposure, .env absent.")
    # Also run whitespace check placeholder
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
