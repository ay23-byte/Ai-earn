"""Product handlers (13-15) — extracted."""
from .helpers import _read, _confirm_yes, _paise, _key


def handle_add_product(products, token, stdin, out):
    title = _read(stdin, out, "Product title (1-200): ")
    desc = _read(stdin, out, "Description (0-2000, empty for none): ")
    version = _read(stdin, out, "Version [A-Za-z0-9._- 1-20]: ").strip()
    price_raw = _read(stdin, out, "Price INR (e.g. 5000 or 2500.50): ").strip()
    price = _paise(price_raw)
    ident = products.create(token, title, desc, version, price, operation_key=_key())
    out.write(f"Created product draft: {ident}\n")


def handle_list_products(products, token, stdin, out):
    status_f = _read(stdin, out, "Filter status [draft/published/archived or empty]: ").strip()
    if status_f == "":
        status_f = None
    cursor = None
    while True:
        entries = products.list(token, after_id=cursor, limit=50, status=status_f)
        for item in entries:
            out.write(f"{item['id']} | {item['title']} | v{item['version']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d} | {item['status']}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No products.\n")


def handle_manage_product(products, token, stdin, out):
    ident = _read(stdin, out, "Product ID: ").strip()
    item = products.get(token, ident)
    out.write(f"Current: {item['title']} | {item['status']} | v{item['version']} | INR {item['price_paise'] // 100}.{item['price_paise'] % 100:02d}\n")
    out.write("Transitions: 1 publish  2 archive\n")
    tchoice = _read(stdin, out, "Choose (1-2) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice == "1":
        out.write("Publish product? This is unverified local draft.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        products.publish(token, ident, operation_key=_key())
        out.write("Product published (unverified).\n")
    elif tchoice == "2":
        out.write("Archive product? This is unverified.\n")
        if not _confirm_yes(stdin, out, "Type YES to confirm: "):
            out.write("No change.\n")
            return
        products.archive(token, ident, operation_key=_key())
        out.write("Product archived.\n")
    else:
        out.write("Invalid choice.\n")
