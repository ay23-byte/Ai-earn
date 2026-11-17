"""Financial assertions handler (choices 1-4) — extracted from tracker_cli._menu."""
from .helpers import _read, _confirm_yes, _paise, _key, _STAGES, _NEXT


def handle_add(service, token, stdin, out):
    title = _read(stdin, out, "Opportunity title (no sensitive data): ")
    amount = _paise(_read(stdin, out, "Proposed INR amount: ").strip())
    ident = service.create(token, title, amount, operation_key=_key())
    out.write(f"Created unverified financial entry: {ident}\n")


def handle_list(service, token, stdin, out):
    cursor = None
    while True:
        entries = service.list(token, after_id=cursor, limit=100)
        for item in entries:
            amount = item["amount_paise"]
            out.write(f"{item['id']} | {item['title']} | INR {amount // 100}.{amount % 100:02d} | {_STAGES[item['stage']]}\n")
        if len(entries) < 100:
            break
        if not _confirm_yes(stdin, out, "More entries? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No entries.\n")


def handle_advance_or_withdraw(service, token, choice, stdin, out):
    ident = _read(stdin, out, "Entry ID: ").strip()
    entry = service.get(token, ident)
    stage = _NEXT.get(entry["stage"]) if choice == "3" else "withdrawn"
    if stage is None or (choice == "4" and entry["stage"] == "withdrawn"):
        out.write("No further change is available.\n")
        return False
    out.write(f"Record {_STAGES[stage]}? This is only your unverified assertion.\n")
    if not _confirm_yes(stdin, out, "Type YES to confirm: "):
        out.write("No change.\n")
        return False
    if choice == "3":
        service.advance(token, ident, stage, confirmed=True, operation_key=_key())
    else:
        service.withdraw(token, ident, confirmed=True, operation_key=_key())
    out.write("Local entry updated; nothing was sent or paid.\n")
    return True
