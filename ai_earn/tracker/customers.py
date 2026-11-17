"""Customer handlers (16-18) — extracted."""
from .helpers import _read, _confirm_yes, _key, _CUST_STATUS_LABELS, _CUST_TRANSITIONS


def handle_add_customer(customers, token, stdin, out):
    name = _read(stdin, out, "Customer name (1-200): ")
    contact = _read(stdin, out, "Contact (1-500): ")
    source = _read(stdin, out, "Source (0-500, empty for none): ")
    ident = customers.create(token, name, contact, source, operation_key=_key())
    out.write(f"Created customer lead: {ident}\n")


def handle_list_customers(customers, token, stdin, out):
    status_f = _read(stdin, out, "Filter status [lead/contacted/converted/rejected or empty]: ").strip()
    if status_f == "":
        status_f = None
    cursor = None
    while True:
        entries = customers.list(token, after_id=cursor, limit=50, status=status_f)
        for item in entries:
            out.write(f"{item['id']} | {item['name']} | {item['contact'][:40]} | {item['status']} | src:{item['source'][:30]}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No customers.\n")


def handle_manage_customer(customers, token, stdin, out):
    ident = _read(stdin, out, "Customer ID: ").strip()
    item = customers.get(token, ident)
    out.write(f"Current: {item['name']} | {item['status']} | {item['contact'][:40]}\n")
    out.write("Transitions: 1 contacted  2 converted  3 rejected\n")
    tchoice = _read(stdin, out, "Choose (1-3) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice not in _CUST_TRANSITIONS:
        out.write("Invalid transition choice.\n")
        return
    target, _evt = _CUST_TRANSITIONS[tchoice]
    out.write(f"Change to {_CUST_STATUS_LABELS.get(target, target)}? This is unverified.\n")
    if not _confirm_yes(stdin, out, "Type YES to confirm: "):
        out.write("No change.\n")
        return
    if target == "contacted":
        customers.contacted(token, ident, operation_key=_key())
    elif target == "converted":
        customers.converted(token, ident, operation_key=_key())
    elif target == "rejected":
        customers.rejected(token, ident, operation_key=_key())
    out.write(f"Customer updated to {target} (unverified).\n")
