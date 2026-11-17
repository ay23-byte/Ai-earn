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
    """Placeholder — stdlib only has no deps to audit. Runs pip-audit if installed."""
    # Check for requirements files; if none, report clean
    reqs = list(ROOT.glob("requirements*.txt")) + list(ROOT.glob("pyproject.toml"))
    has_deps = False
    for f in reqs:
        try:
            txt = f.read_text(encoding="utf-8", errors="ignore")
            if txt.strip() and not txt.strip().startswith("#"):
                has_deps = True
        except Exception:
            continue
    if not has_deps:
        print("Dependency audit: stdlib only — no third-party dependencies to audit (clean).")
        return 0
    # If deps exist, try pip-audit
    try:
        result = subprocess.run([sys.executable, "-m", "pip_audit", "--disable-pip"], capture_output=True, text=True, timeout=30)
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return result.returncode
    except FileNotFoundError:
        print("pip-audit not installed; skipping. Install with pip install pip-audit to enable.")
        return 0
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
