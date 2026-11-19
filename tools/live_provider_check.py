#!/usr/bin/env python3
"""Disposable-account live provider validation (P2-2).

Dry-run by default and contacts no provider. With ``--live``, it performs a
read-only validation using credentials supplied only through environment
variables. Google services use the loopback OAuth callback; GitHub uses its
configured read-only token. Results contain counts/status only, never titles,
message content, credentials, codes, or tokens. Use a disposable account.
"""

from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from collections.abc import Callable, Mapping
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_earn.connectors.base import ConnectorError
from ai_earn.connectors.drive import DriveConnector
from ai_earn.connectors.github import GitHubConnector
from ai_earn.connectors.gmail import GmailConnector


def _valid_owner(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def configuration_status(environ: Mapping[str, str]) -> dict[str, bool]:
    """Report configuration presence without exposing any value."""
    return {
        "AI_EARN_CONNECTOR_LIVE=1": environ.get("AI_EARN_CONNECTOR_LIVE") == "1",
        "AI_EARN_GOOGLE_CLIENT_ID set": bool(environ.get("AI_EARN_GOOGLE_CLIENT_ID")),
        "AI_EARN_GOOGLE_REDIRECT_URI set": bool(environ.get("AI_EARN_GOOGLE_REDIRECT_URI")),
        "AI_EARN_GITHUB_TOKEN set (GitHub only)": bool(environ.get("AI_EARN_GITHUB_TOKEN")),
        "AI_EARN_OWNER_ID valid (or --owner-id)": _valid_owner(environ.get("AI_EARN_OWNER_ID", "")),
    }


def _validate_google_connector(connector: Any, *, owner_id: str, timeout: int, open_browser: bool) -> dict[str, int | str]:
    # Stage-labelled progress: connector errors are categorical messages by
    # design (no codes, tokens, or provider payloads), so naming the stage
    # and message is safe and turns a silent failure into a diagnosis.
    stage = "authorization start"
    try:
        authorization_url = connector.begin_authorization(owner_id=owner_id)
        print("Open this disposable-account authorization URL:")
        print(authorization_url)
        if open_browser:
            webbrowser.open(authorization_url)
        stage = "browser consent / callback (did you finish signing in and click Allow?)"
        code, state = connector.wait_for_authorization_callback(timeout=timeout)
        print("Callback received; exchanging the authorization code...")
        try:
            stage = "token exchange (check client ID/secret and that the consent account is a listed test user)"
            connector.exchange_authorization_code(code=code, state=state, owner_id=owner_id)
            stage = "read-only API call (is the Drive/Gmail API enabled in the same project?)"
            if isinstance(connector, GmailConnector):
                rows = connector.search(query="", limit=10, owner_id=owner_id)
            else:
                rows = connector.list(limit=10, owner_id=owner_id)
            return {"service": connector.connector_id, "items_returned": len(rows), "status": "validated"}
        finally:
            try:
                connector.revoke(owner_id=owner_id)
            except ConnectorError:
                print("Local OAuth credentials were erased; remote revocation could not be confirmed.")
    except ConnectorError as exc:
        print(f"FAILED at stage: {stage}. Reason: {exc}", file=sys.stderr)
        raise


def run_live_validation(
    *,
    service: str,
    owner_id: str,
    timeout: int = 180,
    open_browser: bool = False,
    connector: Any | None = None,
) -> dict[str, int | str]:
    """Run one bounded, read-only live validation and revoke Google access."""
    if service not in {"drive", "gmail", "github"}:
        raise ConnectorError("Unsupported live validation service.")
    if not _valid_owner(owner_id):
        raise ConnectorError("A valid 32-character owner ID is required.")
    if service == "github":
        github = connector or GitHubConnector.from_environment(owner_id=owner_id)
        rows = github.search(query="is:issue is:open", limit=10, owner_id=owner_id)
        return {"service": "github", "items_returned": len(rows), "status": "validated"}
    if service == "gmail":
        google = connector or GmailConnector.from_environment(owner_id=owner_id)
    else:
        google = connector or DriveConnector.from_environment(owner_id=owner_id)
    return _validate_google_connector(google, owner_id=owner_id, timeout=timeout, open_browser=open_browser)


def main(argv: list[str] | None = None, *, connector_factory: Callable[[str], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a disposable live provider configuration.")
    parser.add_argument("--live", action="store_true", help="contact the configured provider (read-only)")
    parser.add_argument("--service", choices=("drive", "gmail", "github"), default="drive")
    parser.add_argument("--owner-id", default=os.environ.get("AI_EARN_OWNER_ID", ""))
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)

    checks = configuration_status(os.environ)
    for label, present in checks.items():
        print(f"{'OK ' if present else 'MISSING'} {label}")
    google_ready = checks["AI_EARN_CONNECTOR_LIVE=1"] and checks["AI_EARN_GOOGLE_CLIENT_ID set"] and checks["AI_EARN_GOOGLE_REDIRECT_URI set"]
    github_ready = checks["AI_EARN_CONNECTOR_LIVE=1"] and checks["AI_EARN_GITHUB_TOKEN set (GitHub only)"]
    ready = github_ready if args.service == "github" else google_ready
    if not args.live:
        print("Dry run only: no provider was contacted. Re-run with --live and a disposable test account.")
        return 0 if ready and _valid_owner(args.owner_id) else 2
    if not ready or not _valid_owner(args.owner_id):
        print("Live validation aborted: configuration incomplete.", file=sys.stderr)
        return 2
    if type(args.timeout) is not int or not 1 <= args.timeout <= 300:
        print("Live validation aborted: invalid timeout.", file=sys.stderr)
        return 2
    try:
        connector = connector_factory(args.service) if connector_factory is not None else None
        result = run_live_validation(
            service=args.service,
            owner_id=args.owner_id,
            timeout=args.timeout,
            open_browser=args.open_browser,
            connector=connector,
        )
    except ConnectorError:
        print("Live validation failed (see FAILED stage above; no credentials were exposed).", file=sys.stderr)
        return 3
    print(f"Validated {result['service']}: {result['items_returned']} item(s) returned; credentials revoked locally.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
