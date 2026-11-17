"""Followups & CSV handlers (23-27) — extracted."""
from .helpers import _read, _confirm_yes, _key


def handle_add_followup(followups, token, stdin, out):
    cid = _read(stdin, out, "Customer ID: ").strip()
    due_raw = _read(stdin, out, "Due at unix timestamp (0 for none): ").strip()
    if due_raw == "":
        due_raw = "0"
    try:
        due = int(due_raw)
    except ValueError:
        raise ValueError("Due must be integer timestamp or 0.")
    note = _read(stdin, out, "Note (0-500, empty for none): ")
    ident = followups.create(token, cid, due, note, operation_key=_key())
    out.write(f"Created followup pending: {ident}\n")


def handle_list_followups(followups, token, stdin, out):
    cid_f = _read(stdin, out, "Filter customer ID (empty for all): ").strip()
    if cid_f == "":
        cid_f = None
    status_f = _read(stdin, out, "Filter status [pending/completed/cancelled or empty]: ").strip()
    if status_f == "":
        status_f = None
    cursor = None
    while True:
        entries = followups.list(token, after_id=cursor, limit=50, customer_id=cid_f, status=status_f)
        for item in entries:
            out.write(f"{item['id']} | cust:{item['customer_id'][:8]} | due:{item['due_at']} | {item['status']} | {item['note'][:40]}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No followups.\n")


def handle_manage_followup(followups, token, stdin, out):
    ident = _read(stdin, out, "Followup ID: ").strip()
    item = followups.get(token, ident)
    out.write(f"Current: cust:{item['customer_id'][:8]} | due:{item['due_at']} | {item['status']} | {item['note'][:40]}\n")
    out.write("Transitions: 1 completed  2 cancelled\n")
    tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice == "1":
        out.write("Complete followup?\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        followups.completed(token, ident, operation_key=_key())
        out.write("Followup completed.\n")
    elif tchoice == "2":
        out.write("Cancel followup?\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        followups.cancelled(token, ident, operation_key=_key())
        out.write("Followup cancelled.\n")
    else:
        out.write("Invalid choice.\n")


def handle_import_csv(customers, token, stdin, out):
    out.write("Paste CSV text (header name,contact,source). End with a line containing only '.'\n")
    lines = []
    while True:
        line = _read(stdin, out, "")
        if line == ".":
            break
        lines.append(line)
    csv_text = "\n".join(lines)
    if not csv_text.strip():
        out.write("No CSV provided.\n")
    else:
        pref = _read(stdin, out, "Operation key prefix (1-64, for idempotency): ").strip()
        if pref == "":
            pref = "csv_import"
        result = customers.import_csv(token, csv_text, operation_key_prefix=pref)
        out.write(f"CSV import: {len(result['created'])} created\n")
        for ident in result["created"]:
            out.write(f"  {ident}\n")


def handle_export_csv(customers, token, stdin, out):
    csv_text = customers.export_csv(token)
    out.write("CSV export:\n")
    out.write(csv_text + "\n")
