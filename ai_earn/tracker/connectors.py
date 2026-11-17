"""Extracted tracker menu handlers for choices 46-48."""
from ai_earn.connectors.gmail import GmailConnector
from ai_earn.connectors.drive import DriveConnector
from ai_earn.connectors.github import GitHubConnector
from .helpers import _read

def handle_connectors(choice, token, stdin, out, **deps):
    opportunities = deps.get("opportunities")
    if choice == "46":
        out.write("Connectors (stub sandbox, no network):\n")
        out.write(" - gmail-stub (Gmail, snippets) | drive-stub (Drive files) | github-stub (GitHub issues) | manual-fallback\n")
        out.write("Auth: api_key 32..128, throttle 10/min per owner, retry 3 backoff, normalize bounded, manual fallback always.\n")
        try:
            for name in ("gmail-stub","drive-stub","github-stub"): 
                out.write(f" {name}: ready (stub)\n")
            out.write(" manual-fallback: ready\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "47":
        which = _read(stdin, out, "Which connector [gmail/drive/github]: ").strip().lower()
        limit_raw = _read(stdin, out, "Limit 1-100 (default 5): ").strip()
        limit = int(limit_raw) if limit_raw else 5
        try:
            key = b"test-connector-api-key-32bytes-long-enough-1234"
            # Need owner_id for throttle: derive from token via auth? Use dummy owner check via analytics owner? Simpler fetch without owner_id
            if which == "gmail":
                c = GmailConnector(api_key=key)
            elif which == "drive":
                c = DriveConnector(api_key=key)
            elif which == "github":
                c = GitHubConnector(api_key=key)
            else:
                out.write("Unknown connector.\n")
                c = None
            if c:
                c.validate_config()
                items = c.fetch(limit=limit)
                out.write(f"Fetched {len(items)} items (normalized, bounded):\n")
                for it in items:
                    out.write(f" - {it['title'][:60]} | {it['source_url'][:60]} | {it['budget_paise']/100:.2f} INR | {it['skill_tags'][:30]}\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "48":
        which = _read(stdin, out, "Import via which [gmail/drive/github/manual]: ").strip().lower()
        prefix = _read(stdin, out, "Operation prefix 1-64 (e.g. import1): ").strip() or "conn_import"
        limit_raw = _read(stdin, out, "Limit 1-100 (default 5): ").strip()
        limit = int(limit_raw) if limit_raw else 5
        try:
            key = b"test-connector-api-key-32bytes-long-enough-1234"
            if which == "gmail":
                c = GmailConnector(api_key=key)
            elif which == "drive":
                c = DriveConnector(api_key=key)
            elif which == "github":
                c = GitHubConnector(api_key=key)
            elif which == "manual":
                from ai_earn.opportunities import ManualConnector
                c = ManualConnector()
            else:
                out.write("Unknown connector.\n")
                c = None
            if c and opportunities is not None:
                ids = opportunities.import_from_connector(token, c, limit=limit, operation_key_prefix=prefix)
                out.write(f"Imported {len(ids)} opportunities (owner-scoped, idempotent by prefix):\n")
                for ident in ids:
                    out.write(f" {ident}\n")
            elif c:
                out.write("Opportunities not available in this state version.\n")
        except Exception as e:
            out.write(str(e) + "\n")

