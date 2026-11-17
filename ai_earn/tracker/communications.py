"""Communications handlers (19-21) — extracted."""
from .helpers import _read, _confirm_yes, _key


def handle_add_comm(communications, token, stdin, out):
    cid = _read(stdin, out, "Customer ID: ").strip()
    chan = _read(stdin, out, "Channel (1-30): ")
    body = _read(stdin, out, "Body (1-2000): ")
    ident = communications.create(token, cid, chan, body, operation_key=_key())
    out.write(f"Created communication draft: {ident}\n")


def handle_list_comms(communications, token, stdin, out):
    cid_f = _read(stdin, out, "Filter customer ID (empty for all): ").strip()
    if cid_f == "":
        cid_f = None
    status_f = _read(stdin, out, "Filter status [draft/sent/archived or empty]: ").strip()
    if status_f == "":
        status_f = None
    cursor = None
    while True:
        entries = communications.list(token, after_id=cursor, limit=50, customer_id=cid_f, status=status_f)
        for item in entries:
            out.write(f"{item['id']} | cust:{item['customer_id'][:8]} | {item['channel']} | {item['status']} | {item['body'][:40]}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No communications.\n")


def handle_manage_comm(communications, token, stdin, out):
    ident = _read(stdin, out, "Communication ID: ").strip()
    item = communications.get(token, ident)
    out.write(f"Current: {item['channel']} | {item['status']} | cust:{item['customer_id'][:8]} | {item['body'][:40]}\n")
    out.write("Transitions: 1 sent  2 archive\n")
    tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice == "1":
        out.write("Send communication? Unverified local record.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        communications.sent(token, ident, operation_key=_key())
        out.write("Communication sent (unverified).\n")
    elif tchoice == "2":
        out.write("Archive communication? Unverified.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        communications.archive(token, ident, operation_key=_key())
        out.write("Communication archived.\n")
    else:
        out.write("Invalid choice.\n")
