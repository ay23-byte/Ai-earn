"""Invoice handlers (28-30) — extracted."""
from .helpers import _read, _confirm_yes, _key


def handle_add_invoice(invoices, token, stdin, out):
    oid = _read(stdin, out, "Order ID (order_drafts): ").strip()
    ident = invoices.create(token, oid, operation_key=_key())
    out.write(f"Created invoice draft: {ident}\n")


def handle_list_invoices(invoices, token, stdin, out):
    status_f = _read(stdin, out, "Filter status [draft/issued/paid/overdue/void or empty]: ").strip()
    if status_f == "":
        status_f = None
    oid_f = _read(stdin, out, "Filter order ID (empty for all): ").strip()
    if oid_f == "":
        oid_f = None
    cursor = None
    while True:
        entries = invoices.list(token, after_id=cursor, limit=50, status=status_f, order_id=oid_f)
        for item in entries:
            pdf_note = "PDF" if item['pdf_hash'] is not None else "no PDF"
            out.write(f"{item['id']} | {item['invoice_number']} | order:{item['order_id'][:8]} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['status']} | {pdf_note}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No invoices.\n")


def handle_manage_invoice(invoices, token, stdin, out):
    ident = _read(stdin, out, "Invoice ID: ").strip()
    item = invoices.get(token, ident)
    pdf_note = "PDF stub available" if item['pdf_hash'] is not None else "no PDF (draft/void)"
    out.write(f"Current: {item['invoice_number']} | order:{item['order_id'][:8]} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['status']} | {pdf_note}\n")
    out.write("Transitions: 1 issued  2 paid  3 overdue  4 void  5 pdf\n")
    tchoice = _read(stdin, out, "Choose (1-5) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice == "1":
        out.write("Issue invoice? PDF stub hash will be stored.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        invoices.issued(token, ident, operation_key=_key())
        out.write("Invoice issued (PDF stub).\n")
    elif tchoice == "2":
        out.write("Mark paid?\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        invoices.paid(token, ident, operation_key=_key())
        out.write("Invoice paid.\n")
    elif tchoice == "3":
        out.write("Mark overdue?\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        invoices.overdue(token, ident, operation_key=_key())
        out.write("Invoice overdue.\n")
    elif tchoice == "4":
        out.write("Void invoice?\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        invoices.voided(token, ident, operation_key=_key())
        out.write("Invoice voided.\n")
    elif tchoice == "5":
        try:
            stub = invoices.pdf_stub(token, ident)
            out.write(f"PDF stub ({len(stub)} bytes):\n")
            out.write(stub.decode('utf-8', errors='replace') + "\n")
            out.write(f"Hash {item['pdf_hash'].hex() if item['pdf_hash'] else 'none'}\n")
        except Exception as e:
            out.write(str(e) + "\n")
    else:
        out.write("Invalid choice.\n")
