"""Service catalog & portfolio handlers (choices 9-12) — extracted from tracker_cli._menu."""
from .helpers import _read, _confirm_yes, _paise, _key


def handle_add_service(services, token, stdin, out):
    title = _read(stdin, out, "Service title (1-200): ")
    scope = _read(stdin, out, "Scope (0-2000, empty for none): ")
    deliverable = _read(stdin, out, "Deliverable (1-500): ")
    price_raw = _read(stdin, out, "Price INR (e.g. 5000 or 2500.50): ").strip()
    price = _paise(price_raw)
    rev_raw = _read(stdin, out, "Revisions (0-100, default 1): ").strip()
    rev = int(rev_raw) if rev_raw != "" else 1
    ident = services.create(token, title, scope, deliverable, price, rev, operation_key=_key())
    out.write(f"Created service draft: {ident}\n")


def handle_list_services(services, token, stdin, out):
    status_f = _read(stdin, out, "Filter status [draft/published/archived or empty]: ").strip()
    if status_f == "":
        status_f = None
    cursor = None
    while True:
        entries = services.list(token, after_id=cursor, limit=50, status=status_f)
        for item in entries:
            out.write(f"{item['id']} | {item['title']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d} | rev:{item['revisions']} | {item['status']} | scope:{item['scope'][:40]}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No services.\n")


def handle_manage_service(services, token, stdin, out):
    ident = _read(stdin, out, "Service ID: ").strip()
    item = services.get(token, ident)
    out.write(f"Current: {item['title']} | {item['status']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d}\n")
    out.write("Transitions: 1 publish  2 archive\n")
    tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice == "1":
        out.write("Publish service? This is unverified local draft.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        services.publish(token, ident, operation_key=_key())
        out.write("Service published (unverified).\n")
    elif tchoice == "2":
        out.write("Archive service? This is unverified.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        services.archive(token, ident, operation_key=_key())
        out.write("Service archived.\n")
    else:
        out.write("Invalid choice.\n")


def handle_portfolio(services, portfolio, token, stdin, out):
    out.write("Portfolio: 1 Add item  2 List items\n")
    sub = _read(stdin, out, "Choice (1-2): ").strip()
    if sub == "1":
        sid = _read(stdin, out, "Service ID: ").strip()
        title = _read(stdin, out, "Portfolio title (1-200): ")
        desc = _read(stdin, out, "Description (0-2000): ")
        pid = portfolio.create(token, sid, title, desc, operation_key=_key())
        out.write(f"Created portfolio item: {pid}\n")
    elif sub == "2":
        sid = _read(stdin, out, "Service ID (empty for all): ").strip()
        if sid == "":
            sid = None
        cursor = None
        while True:
            entries = portfolio.list(token, service_id=sid, after_id=cursor, limit=50)
            for item in entries:
                out.write(f"{item['id']} | svc:{item['service_id'][:8]} | {item['title']} | {item['description'][:60]}\n")
            if len(entries) < 50:
                break
            if not _confirm_yes(stdin, out, "More? Type YES: "):
                break
            cursor = entries[-1]["id"]
        if cursor is None and not entries:
            out.write("No portfolio items.\n")
    else:
        out.write("Invalid portfolio choice.\n")
