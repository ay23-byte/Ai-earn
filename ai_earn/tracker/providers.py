"""Provider handlers (31-33) — extracted."""
from .helpers import _read, _confirm_yes, _paise, _key


def handle_add_provider(provider_orders, token, stdin, out):
    provider = _read(stdin, out, "Provider [razorpay/stripe]: ").strip() or "razorpay"
    amt_raw = _read(stdin, out, "Amount INR (e.g. 5000 or 2500.50): ").strip()
    amt = _paise(amt_raw)
    ident = provider_orders.create_order(token, amt, operation_key=_key())
    out.write(f"Created provider order ({provider}): {ident}\n")


def handle_list_providers(provider_orders, token, stdin, out):
    status_f = _read(stdin, out, "Filter status [created/authorized/captured/settled/refunded/failed or empty]: ").strip()
    if status_f == "":
        status_f = None
    cursor = None
    while True:
        entries = provider_orders.list(token, after_id=cursor, limit=50, status=status_f)
        for item in entries:
            out.write(f"{item['id']} | {item['provider_order_id']} | {item['provider']}/{item['mode']} | INR {item['amount_paise'] // 100}.{item['amount_paise'] % 100:02d} | {item['status']}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No provider orders.\n")


def handle_webhook(provider_orders, token, stdin, out):
    out.write("Webhook: paste raw JSON body (single line). Signature must be HMAC of raw body.\n")
    body_raw = _read(stdin, out, "Raw body: ")
    sig = _read(stdin, out, "Signature (hex for razorpay, t=...,v1=... for stripe): ").strip()
    try:
        raw = body_raw.encode('utf-8')
        result = provider_orders.handle_webhook(token, raw, sig, operation_key=_key())
        out.write(f"Webhook processed: event {result['event_id']} -> {result['new_status']} for order {result['order_id'][:8]}\n")
    except Exception as e:
        out.write(str(e) + "\n")
