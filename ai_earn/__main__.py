"""CLI entrypoint. No network service is created."""

import json
import os
import sys
from typing import Mapping, Sequence, TextIO

from .config import ConfigurationError, load_settings
from .events import Event, write_event
from .responses import failure, success
from .status import current_status


def main(args: Sequence[str], environ: Mapping[str, str], out: TextIO, err: TextIO) -> int:
    if len(args) >= 1 and args[0] == "doctor":
        from .doctor_cli import run as doctor_run
        # `python -m ai_earn doctor` or `python -m ai_earn doctor --plan-init`
        # Accept any trailing args; pass them through without exposing secrets.
        return doctor_run(args[1:], environ, out, err)

    if len(args) >= 1 and args[0] == "health":
        from .health_cli import run as health_run
        return health_run(args[1:], environ, out, err)

    if len(args) == 2 and args[0] == "tracker":
        from .tracker_cli import run
        return run(args[1], environ, sys.stdin, out, err)

    if len(args) == 2 and args[0] == "auth":
        from .auth_cli import run
        return run(args[1], environ, sys.stdin, out, err)

    if len(args) == 2 and args[0] == "dashboard":
        from .dashboard_cli import run
        return run(args[1], environ, sys.stdin, out, err)

    if len(args) == 2 and args[0] == "data":
        from .data_cli import run as data_run
        return data_run(args[1], environ, sys.stdin, out, err)

    if len(args) == 2 and args[0] == "production":
        from .production_config import production_summary
        import json as _j

        out.write(_j.dumps(production_summary(environ), indent=2, sort_keys=True) + "\n")
        return 0

    if list(args) != ["status"]:
        write_event(Event.INVALID_REQUEST, err)
        result = failure(
            "invalid_command",
            "Usage: python -m ai_earn status | auth {register|login|recover} | tracker {init|open} | dashboard snapshot | doctor [--plan-init|--json] | health [--json] [--with-backups] | data {export|delete} | production",
        )
        out.write(json.dumps(result) + "\n")
        return 2

    try:
        settings = load_settings(environ)
        result = success(current_status(settings))
        write_event(Event.STATUS_REQUESTED, err, settings.log_level)
    except ConfigurationError as exc:
        write_event(Event.INVALID_CONFIGURATION, err)
        result = failure("invalid_configuration", str(exc))
        out.write(json.dumps(result) + "\n")
        return 2
    except Exception:
        # Never log or return exception text: it may contain secrets.
        write_event(Event.INTERNAL_ERROR, err)
        result = failure("internal_error", "An unexpected error occurred.")
        out.write(json.dumps(result) + "\n")
        return 1

    out.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], os.environ, sys.stdout, sys.stderr))
