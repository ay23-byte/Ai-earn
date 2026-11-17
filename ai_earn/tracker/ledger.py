"""Ledger handler (22) — extracted."""
from .helpers import _read, _confirm_yes, _paise, _key


def handle_ledger(ledger, token, stdin, out):
    out.write("Ledger: 1 Add entry  2 List entries  3 Net\n")
    sub = _read(stdin, out, "Choice (1-3): ").strip()
    if sub == "1":
        kind = _read(stdin, out, "Kind [income/expense/refund/fee]: ").strip()
        amt_raw = _read(stdin, out, "Amount INR (e.g. 5000 or 2500.50): ").strip()
        amt = _paise(amt_raw)
        note = _read(stdin, out, "Note (0-500, empty for none): ")
        ident = ledger.add(token, kind, amt, note, operation_key=_key())
        out.write(f"Created ledger entry: {ident}\n")
    elif sub == "2":
        kind_f = _read(stdin, out, "Filter kind [income/expense/refund/fee or empty]: ").strip()
        if kind_f == "":
            kind_f = None
        cursor = None
        while True:
            entries = ledger.list(token, after_id=cursor, limit=50, kind=kind_f)
            for item in entries:
                out.write(f"{item['id']} | {item['kind']} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['note'][:40]}\n")
            if len(entries) < 50:
                break
            if not _confirm_yes(stdin, out, "More? Type YES: "):
                break
            cursor = entries[-1]["id"]
        if cursor is None and not entries:
            out.write("No ledger entries.\n")
    elif sub == "3":
        result = ledger.net(token)
        out.write(f"Ledger net: INR {result['net_paise'] // 100}.{result['net_paise'] % 100:02d} (income {result['totals']['income']//100}.{result['totals']['income']%100:02d} - expense {result['totals']['expense']//100}.{result['totals']['expense']%100:02d} - refund {result['totals']['refund']//100}.{result['totals']['refund']%100:02d} - fee {result['totals']['fee']//100}.{result['totals']['fee']%100:02d}) count:{result['count']}\n")
    else:
        out.write("Invalid ledger choice.\n")
