"""Exact integer arithmetic shared by finance and reporting services.

These helpers calculate from supplied persisted aggregates; they do not
authenticate, reconcile, or upgrade a record's verification status.
"""


def net_cash_paise(
    income_paise: int,
    refunds_paise: int,
    fees_paise: int,
    expenses_paise: int,
) -> int:
    """Return income less refunds, fees, and expenses without float rounding."""
    components = (income_paise, refunds_paise, fees_paise, expenses_paise)
    if any(type(value) is not int or value < 0 for value in components):
        raise ValueError("Financial aggregate components must be non-negative integer paise.")
    return income_paise - refunds_paise - fees_paise - expenses_paise
