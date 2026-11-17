"""CLI for `python -m ai_earn health`. No secrets are read or logged."""
import json
from typing import Mapping, Sequence, TextIO

from .health import check, check_with_backups


def run(args: Sequence[str], environ: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    # Accept --json or --with-backups (both optional, order independent)
    want_json = "--json" in args
    with_backups = "--with-backups" in args
    # Reject unknown flags (allow only those two)
    for a in args:
        if a not in ("--json", "--with-backups"):
            err.write("Usage: python -m ai_earn health [--json] [--with-backups]\n")
            return 2

    fn = check_with_backups if with_backups else check
    payload = fn(environ)

    if want_json:
        out.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    else:
        # Human-readable, no secrets
        out.write(f"environment: {payload.get('environment')}\n")
        out.write(f"production_ready: {payload.get('production_ready')}\n")
        out.write(f"ok: {payload.get('ok')}\n")
        for name in ("config", "auth", "state", "version", "backups"):
            chk = payload.get("checks", {}).get(name)
            if chk is None:
                continue
            out.write(f"{name}: {chk.get('status')} — {chk.get('detail')}\n")
        if not payload.get("ok"):
            out.write("Next: python -m ai_earn doctor --plan-init\n")
    # Exit code: 0 when ok, 1 when not ok but valid check, 2 when config invalid
    if payload["checks"].get("config", {}).get("status") == "invalid_configuration":
        return 2
    return 0 if payload.get("ok") else 1
