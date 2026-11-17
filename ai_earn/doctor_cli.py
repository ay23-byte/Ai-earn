"""Read-only CLI for database diagnostics and recovery planning.

This CLI never creates, migrates, replaces or truncates a database.
It is safe to run even when stores are missing, damaged or misconfigured
and never logs or prints secrets, tokens or credential material.
"""

import json
from typing import Mapping, Sequence, TextIO

from .events import Event, write_event

from .doctor import diagnose


def _report_to_dict(report: dict) -> dict:
    """Convert StoreReport dataclasses to plain JSON-serializable dicts."""
    auth = report.get("auth")
    state = report.get("state")
    return {
        "environment": report.get("environment"),
        "problems": list(report.get("problems", [])),
        "actions": list(report.get("actions", [])),
        "auth": {
            "variable": auth.variable,
            "status": auth.status,
            "detail": auth.detail,
            "schema_version": auth.schema_version,
            "account_count": auth.account_count,
        } if auth else None,
        "state": {
            "variable": state.variable,
            "status": state.status,
            "detail": state.detail,
            "schema_version": state.schema_version,
            "account_count": state.account_count,
        } if state else None,
        "stale": report.get("stale"),
    }


def _print_plan(out: TextIO) -> None:
    out.write("Database recovery plan (no files are changed by doctor):\n")
    out.write("\n")
    out.write("1. Inspect the configured private directory and both files:\n")
    out.write("   - Verify AI_EARN_AUTH_DB and AI_EARN_STATE_DB point to absolute paths\n")
    out.write("     inside ONE newly created, owner-only private directory (POSIX 0700,\n")
    out.write("     Windows protected ACL). Never reuse an untrusted or shared directory.\n")
    out.write("   - Run: python -m ai_earn doctor   to see current file statuses.\n")
    out.write("\n")
    out.write("2. If the authentication database is MISSING:\n")
    out.write("   - Do NOT run 'auth register' over the old path to replace it silently.\n")
    out.write("   - Restore the file from a known-good backup to the same path,\n")
    out.write("     then verify permissions and re-run doctor.\n")
    out.write("   - Only if no backup exists and you accept losing the account,\n")
    out.write("     create a new private directory and register a disposable test account.\n")
    out.write("\n")
    out.write("3. If the state database is MISSING while the auth database exists:\n")
    out.write("   - This is expected after an interrupted first-time setup.\n")
    out.write("   - Inspect the private directory: ensure no partial files, journals,\n")
    out.write("     -wal/-shm sidecars or stale before-v*.sqlite3 backups remain.\n")
    out.write("   - To initialize a NEW state store, run with explicit confirmation:\n")
    out.write("       python -m ai_earn tracker init\n")
    out.write("     It will ask for YES, create a new state file and 13 migration backups\n")
    out.write("     (before-v2 .. before-v14), never overwriting an existing file.\n")
    out.write("   - If tracker init refuses because files already exist, do NOT delete\n")
    out.write("     them automatically. Inspect or restore from backup instead.\n")
    out.write("\n")
    out.write("4. If either database reports unsupported/incompatible/damaged/empty:\n")
    out.write("   - Do not delete or overwrite it. Inspect integrity and permissions,\n")
    out.write("     restore from a trusted backup to a NEW path for comparison,\n")
    out.write("     and seek recovery before any approved migration.\n")
    out.write("\n")
    out.write("5. After stores diagnose as OK:\n")
    out.write("   - Verify with: python -m ai_earn dashboard snapshot  (interactive)\n")
    out.write("   - Then load the generated JSON via dashboard/connected.html (local file).\n")
    out.write("\n")
    out.write("All steps require absolute paths, a real terminal, and use only disposable\n")
    out.write("test data until Windows verification and independent backup/restore audits pass.\n")


def run(args: Sequence[str], environ: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    # --check-stale handling (read-only, compares auth_audit timestamps)
    # Supported: --check-stale, --check-stale --json, --check-stale <abs-path>, --check-stale <path> --json
    _raw = list(args)
    _check_stale = False
    _stale_path = None
    _want_json_stale = False
    # Simple parse: look for --check-stale / --check_stale / -check-stale
    stale_flags = {"--check-stale", "--check_stale", "-check-stale", "check-stale", "--stale"}
    json_flags = {"--json", "-j", "json"}
    remaining = []
    i = 0
    while i < len(_raw):
        a = _raw[i].strip()
        if a in stale_flags:
            _check_stale = True
            # peek next arg as path if it looks like absolute path
            if i + 1 < len(_raw) and _raw[i+1].strip().startswith("/"):
                _stale_path = _raw[i+1].strip()
                i += 1
            elif i + 1 < len(_raw) and not _raw[i+1].strip().startswith("-") and _raw[i+1].strip() not in json_flags and _raw[i+1].strip() not in stale_flags:
                # treat next as path if not a flag
                cand = _raw[i+1].strip()
                if cand and cand not in {"diagnose", "--diagnose", "--plan-init", "plan-init"}:
                    # Only accept if it contains / or .sqlite
                    if "/" in cand or cand.endswith(".sqlite3"):
                        _stale_path = cand
                        i += 1
        elif a in json_flags:
            _want_json_stale = True
            remaining.append(a)
        else:
            remaining.append(a)
        i += 1
    if _check_stale:
        # Build environ override with stale path if provided
        eff_environ = dict(environ)
        if _stale_path:
            eff_environ["AI_EARN_AUTH_BACKUP"] = _stale_path
        # Perform diagnose and isolate stale report
        try:
            from .doctor import diagnose as _diag
            report = _diag(eff_environ)
            payload = _report_to_dict(report)
            stale = report.get("stale") or {}
        except Exception:
            err.write("Doctor stale check could not complete; no files were changed.\n")
            return 1
        if _want_json_stale or "--json" in remaining:
            out.write(json.dumps({"stale": stale, "environment": payload.get("environment")}, indent=2, sort_keys=True) + "\n")
            return 0 if stale.get("status") != "stale" else 1
        # Human-readable
        out.write("Doctor stale check (read-only, compares auth_audit MAX(occurred_at)):\n")
        out.write(f"  status: {stale.get('status')}\n")
        out.write(f"  detail: {stale.get('detail')}\n")
        if stale.get("delta_seconds") is not None:
            out.write(f"  delta_seconds: {stale.get('delta_seconds')}\n")
        if stale.get("status") == "stale":
            out.write("  action: revoke sessions on restored copy and rotate recovery code (confirmed=True)\n")
            out.write("  hint: python -c \"from ai_earn.backup import revoke_all_sessions; revoke_all_sessions(Path('/path/to/restored.sqlite3'), confirmed=True)\"\n")
        out.write("\n--- json ---\n")
        out.write(json.dumps({"stale": stale}, indent=2, sort_keys=True) + "\n")
        return 0 if stale.get("status") != "stale" else 1
        # end check-stale

    # Accept both 'doctor' with no args and explicit sub-options.
    # Canonical: `python -m ai_earn doctor`  -> diagnose
    #            `python -m ai_earn doctor --plan-init` -> plan
    # Also tolerate `diagnose`, `plan-init`, `--plan_init`, etc. for convenience.
    normalized = [a.strip() for a in args]
    # Filter out empty strings that could come from split handling
    normalized = [a for a in normalized if a != ""]

    if not normalized or normalized == ["diagnose"] or normalized == ["--diagnose"]:
        try:
            write_event(Event.DOCTOR_REQUESTED, err)
            report = diagnose(environ)
            payload = _report_to_dict(report)
        except Exception:
            write_event(Event.INTERNAL_ERROR, err)
            err.write("Doctor could not complete diagnosis; no files were changed.\n")
            return 1
        # Human-readable summary to out (visible in terminal)
        out.write("Doctor diagnosis (read-only, no changes made):\n")
        out.write(f"  environment: {payload['environment']}\n")
        for key in ("auth", "state"):
            info = payload[key]
            if info:
                out.write(f"  {info['variable']}: {info['status']} — {info['detail']}\n")
                if info["schema_version"] is not None:
                    out.write(f"    schema_version: {info['schema_version']}\n")
                if info["account_count"] is not None:
                    out.write(f"    account_count: {info['account_count']}\n")
        if payload["problems"]:
            out.write("\nProblems:\n")
            for problem in payload["problems"]:
                out.write(f"  - {problem}\n")
        if payload["actions"]:
            out.write("\nRecommended actions:\n")
            for action in payload["actions"]:
                out.write(f"  - {action}\n")
        # Machine-readable JSON to out as second block (still safe, no secrets)
        # Write to the same stream after human text separated by marker.
        out.write("\n--- json ---\n")
        out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        # Exit codes: 0 if both OK, 2 if configuration/problems, 1 for unexpected?
        # We expose status via JSON; always return 0 for successful diagnosis
        # so callers can parse JSON. Problems are data, not CLI failure.
        return 0

    if normalized in (["--plan-init"], ["plan-init"], ["--plan_init"], ["plan_init"], ["--plan-init", "diagnose"], ["diagnose", "--plan-init"]):
        # Also handle combined: doctor diagnose --plan-init
        try:
            write_event(Event.DOCTOR_PLAN_REQUESTED, err)
        except Exception:
            pass
        _print_plan(out)
        return 0

    # Support `doctor --json` variant that outputs only JSON (no human header)
    if normalized in (["--json"], ["-j"], ["json"]):
        try:
            write_event(Event.DOCTOR_REQUESTED, err)
            report = diagnose(environ)
            payload = _report_to_dict(report)
        except Exception:
            write_event(Event.INTERNAL_ERROR, err)
            err.write("Doctor could not complete diagnosis; no files were changed.\n")
            return 1
        out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0

    if normalized in (["--help"], ["-h"], ["help"]):
        out.write("Usage: python -m ai_earn doctor [--plan-init|--json|--check-stale]\n")
        out.write("  No arguments  : diagnose both stores (read-only) and show actions.\n")
        out.write("  --plan-init   : show explicit, confirmation-required recovery steps.\n")
        out.write("  --json        : emit machine-readable JSON only.\n")
        out.write("  --check-stale : compare auth_audit MAX(occurred_at) between AI_EARN_AUTH_DB and AI_EARN_AUTH_BACKUP (or path arg)\n")
        out.write("                : stale -> requires session revocation + recovery code rotation\n")
        return 0

    # Single dash handling for combined args like ["diagnose", "--json"]
    if len(normalized) == 2 and set(normalized) == {"diagnose", "--json"}:
        try:
            write_event(Event.DOCTOR_REQUESTED, err)
            report = diagnose(environ)
            payload = _report_to_dict(report)
        except Exception:
            write_event(Event.INTERNAL_ERROR, err)
            err.write("Doctor could not complete diagnosis; no files were changed.\n")
            return 1
        out.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0

    err.write("Usage: python -m ai_earn doctor [--plan-init|--json|--check-stale]\n")
    return 2
