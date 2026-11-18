"""Tracker helpers — pure utilities and constants (extracted from tracker_cli).

No payment is verified, received or processed.
"""
import re
import secrets

from ai_earn.state_storage import LATEST_SCHEMA_VERSION, StateStorage, StateStorageError

_STAGES = {"potential": "potential opportunity", "quote_asserted": "quote (unverified)",
           "contract_asserted": "contract (unverified)", "invoice_asserted": "invoice (unverified)",
           "initiated_asserted": "payment initiated (unverified, not received)", "withdrawn": "withdrawn"}
_NEXT = {"potential": "quote_asserted", "quote_asserted": "contract_asserted",
         "contract_asserted": "invoice_asserted", "invoice_asserted": "initiated_asserted"}

_OPP_STATUS_LABELS = {
    "discovered": "discovered (unverified)",
    "vetted": "vetted (unverified)",
    "pursuing": "pursuing (unverified)",
    "archived": "archived",
    "dismissed": "dismissed",
}
_OPP_TRANSITIONS = {
    "1": ("vetted", "opportunity_vetted"),
    "2": ("pursuing", "opportunity_pursuing"),
    "3": ("archived", "opportunity_archived"),
    "4": ("dismissed", "opportunity_dismissed"),
}

_PROD_STATUS_LABELS = {
    "draft": "draft (unverified)",
    "published": "published (unverified)",
    "archived": "archived",
}
_CUST_STATUS_LABELS = {
    "lead": "lead (unverified)",
    "contacted": "contacted (unverified)",
    "converted": "converted (unverified)",
    "rejected": "rejected",
}
_CUST_TRANSITIONS = {
    "1": ("contacted", "customer_contacted"),
    "2": ("converted", "customer_converted"),
    "3": ("rejected", "customer_rejected"),
}
_COMM_STATUS_LABELS = {
    "draft": "draft (unverified)",
    "sent": "sent (unverified)",
    "archived": "archived",
}
_LEDGER_KINDS = {"income", "expense", "refund", "fee"}
_FOLLOWUP_STATUS_LABELS = {
    "pending": "pending",
    "completed": "completed",
    "cancelled": "cancelled",
}
_INVOICE_STATUS_LABELS = {
    "draft": "draft (unverified)",
    "issued": "issued (PDF stub)",
    "paid": "paid (unverified)",
    "overdue": "overdue (unverified)",
    "void": "void",
}
_PROVIDER_STATUS_LABELS = {
    "created": "created (sandbox)",
    "authorized": "authorized (HMAC)",
    "captured": "captured (HMAC)",
    "settled": "settled (sandbox)",
    "refunded": "refunded",
    "failed": "failed",
}
_AI_STATUS_LABELS = {
    "pending": "pending (awaiting approval)",
    "approved": "approved",
    "rejected": "rejected",
    "expired": "expired",
    "revoked": "revoked",
}



def _read(stdin, out, prompt):
    out.write(prompt)
    out.flush()
    line = stdin.readline()
    if not line:
        raise EOFError
    return line.rstrip("\r\n")


def _confirm_yes(stdin, out, prompt: str) -> bool:
    """Require exact uppercase YES; give helpful hint on case-variant.

    Returns True only for exactly "YES". For "yes"/"Yes"/"yES" etc, writes a
    tip and returns False so callers show "No change" without creating files.
    """
    raw = _read(stdin, out, prompt).strip()
    if raw == "YES":
        return True
    if raw.lower() == "yes":
        out.write(f"Tip: use exact uppercase YES (you typed '{raw}'). No change.\n")
    return False


def _paise(value):
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,10})(?:\.[0-9]{1,2})?", value) is None:
        raise ValueError("Enter a non-negative INR amount, with at most two decimal places.")
    whole, _, fractional = value.partition(".")
    amount = int(whole) * 100 + int((fractional + "00")[:2])
    if amount > 1_000_000_000_000:
        raise ValueError("Amount exceeds the supported limit.")
    return amount


def _key():
    return "tracker_" + secrets.token_hex(24)


def _initialize_state(path):
    # Only initialize a new file; never silently upgrade an existing database.
    # Use LATEST_SCHEMA_VERSION so backup chain stays correct as schema evolves.
    backups = [path.with_name(path.name + f".before-v{version}.sqlite3") for version in range(2, LATEST_SCHEMA_VERSION + 1)]
    if path.exists() or path.is_symlink() or any(p.exists() or p.is_symlink() for p in backups):
        raise StateStorageError("State or migration backup already exists; no initialization was attempted.")
    state = StateStorage(path, create=True, confirmed=True)
    for version, backup in zip(range(2, LATEST_SCHEMA_VERSION + 1), backups):
        getattr(state, f"migrate_to_v{version}")(backup, confirmed=True)
    return state


def _csv_preview(text: str, limit: int = 500) -> str:
    sample = text.replace("\n", "\\n")[:limit]
    return sample + ("..." if len(text) > limit else "")


