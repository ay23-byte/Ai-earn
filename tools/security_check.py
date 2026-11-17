#!/usr/bin/env python3
"""Security checks — secret scan and dependency audit.

Usage:
  python -m tools.security_check          # scan repository secrets and DB exposure
  python -m tools.security_check --audit  # audit the active Python environment
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
    for path in ROOT.rglob("*.py"):
        path_text = str(path)
        if ".venv" in path_text or "__pycache__" in path_text or ".git" in path_text:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, label in FORBIDDEN:
            if re.search(pattern, source):
                offenders.append(f"{path}: {label} ({pattern})")

    if (ROOT / ".env").exists():
        offenders.append(".env file present in repo — keep secrets outside the repository")

    for path in (ROOT / "dashboard" / "app.js", ROOT / "dashboard" / "connected.html"):
        if path.exists() and "0.0.0.0" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(f"{path}: must not bind 0.0.0.0 with local SQLite")
    return offenders


def _declares_third_party_dependencies() -> bool:
    """Return whether project or requirements files declare third-party packages."""
    for requirements in ROOT.glob("requirements*.txt"):
        try:
            for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith(("#", "-")):
                    return True
        except OSError:
            continue

    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        return False

    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError, ModuleNotFoundError):
        # Do not guess from arbitrary TOML text. Requirements files remain authoritative
        # on Python versions where the standard-library TOML parser is unavailable.
        return False

    project = data.get("project", {})
    dependencies = project.get("dependencies") or data.get("dependencies") or []
    if isinstance(dependencies, list) and any(str(item).strip() for item in dependencies):
        return True

    optional = project.get("optional-dependencies", {})
    return isinstance(optional, dict) and any(
        isinstance(items, list) and any(str(item).strip() for item in items)
        for items in optional.values()
    )


def audit_dependencies() -> int:
    """Audit the active environment; fail truthfully on missing tooling or audit errors."""
    if not _declares_third_party_dependencies():
        print("Dependency audit: no third-party dependencies declared — clean.")
        return 0

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except FileNotFoundError:
        print(
            "Dependency audit could not start: pip-audit is not installed. "
            "Install the pinned development tools with "
            "'python -m pip install -r requirements-dev.txt'.",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print("Dependency audit failed: pip-audit exceeded the 120-second timeout.", file=sys.stderr)
        return 1

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")

    if result.returncode != 0:
        print(
            f"Dependency audit failed with exit code {result.returncode}. "
            "Review the pip-audit output above; this may be a vulnerability finding "
            "or an audit execution/resolution error.",
            file=sys.stderr,
        )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if "--audit" in arguments:
        return audit_dependencies()

    offenders = scan_secrets()
    if offenders:
        for offender in offenders:
            print(f"FAIL: {offender}", file=sys.stderr)
        return 1

    print("Security scan passed: no forbidden secret patterns, no 0.0.0.0 DB exposure, .env absent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
