"""Opportunity handlers (choices 5-8) — extracted from tracker_cli._menu."""
from .helpers import _read, _confirm_yes, _paise, _key, _OPP_STATUS_LABELS, _OPP_TRANSITIONS


def handle_create(opportunities, token, stdin, out):
    title = _read(stdin, out, "Opportunity title (1-200, no control chars): ")
    url = _read(stdin, out, "Source URL (http:// or https://): ").strip()
    budget_raw = _read(stdin, out, "Budget INR amount (e.g. 5000 or 5000.50): ").strip()
    budget = _paise(budget_raw)
    deadline_raw = _read(stdin, out, "Deadline unix timestamp (0 for none, or future seconds): ").strip()
    if deadline_raw == "":
        deadline_raw = "0"
    try:
        deadline = int(deadline_raw)
    except ValueError:
        raise ValueError("Deadline must be an integer timestamp or 0.")
    risk = _read(stdin, out, "Risk level [low/medium/high/unknown] (default unknown): ").strip().lower()
    if risk == "":
        risk = "unknown"
    tags = _read(stdin, out, "Skill tags (comma separated, 0-500 chars, empty for none): ")
    ident = opportunities.create(
        token,
        title,
        url,
        budget,
        operation_key=_key(),
        deadline_at=deadline,
        risk_level=risk,
        skill_tags=tags,
        source_type="manual",
        connector_id=None,
    )
    out.write(f"Created unverified opportunity (discovered): {ident}\n")
    out.write("Verification: unverified, budget_not_revenue. Use vet/pursue to advance.\n")


def handle_list(opportunities, token, stdin, out):
    out.write("Listing opportunities (owner-scoped, id-ordered). Filters optional.\n")
    status_f = _read(stdin, out, "Filter status [discovered/vetted/pursuing/archived/dismissed or empty]: ").strip()
    if status_f == "":
        status_f = None
    risk_f = _read(stdin, out, "Filter risk [low/medium/high/unknown or empty]: ").strip()
    if risk_f == "":
        risk_f = None
    cursor = None
    while True:
        entries = opportunities.list(token, after_id=cursor, limit=50, status=status_f, risk_level=risk_f)
        for item in entries:
            out.write(f"{item['id']} | {item['title']} | {item['source_url'][:60]} | INR {item['budget_paise'] // 100}.{item['budget_paise'] % 100:02d} | {_OPP_STATUS_LABELS.get(item['status'], item['status'])} | risk:{item['risk_level']} | skills:{item['skill_tags'][:40]}\n")
        if len(entries) < 50:
            break
        if not _confirm_yes(stdin, out, "More? Type YES: "):
            break
        cursor = entries[-1]["id"]
    if cursor is None and not entries:
        out.write("No opportunities.\n")


def handle_manage(opportunities, token, stdin, out):
    ident = _read(stdin, out, "Opportunity ID: ").strip()
    item = opportunities.get(token, ident)
    out.write(f"Current: {item['title']} | {_OPP_STATUS_LABELS.get(item['status'], item['status'])} | risk:{item['risk_level']} | budget INR {item['budget_paise'] // 100}.{item['budget_paise'] % 100:02d}\n")
    out.write("Transitions: 1 vetted  2 pursuing  3 archived  4 dismissed\n")
    tchoice = _read(stdin, out, "Choose transition (1-4) or empty to cancel: ").strip()
    if tchoice == "":
        out.write("No change.\n")
        return
    if tchoice not in _OPP_TRANSITIONS:
        out.write("Invalid transition choice.\n")
        return
    target, _event = _OPP_TRANSITIONS[tchoice]
    out.write(f"Change to {_OPP_STATUS_LABELS.get(target, target)}? This is unverified.\n")
    if not _confirm_yes(stdin, out, "Type YES to confirm: "):
        out.write("No change.\n")
        return
    if target == "vetted":
        opportunities.vetted(token, ident, operation_key=_key())
    elif target == "pursuing":
        opportunities.pursuing(token, ident, operation_key=_key())
    elif target == "archived":
        opportunities.archived(token, ident, operation_key=_key())
    elif target == "dismissed":
        opportunities.dismissed(token, ident, operation_key=_key())
    out.write(f"Opportunity updated to {target} (unverified).\n")


def handle_ranked(opportunities, token, stdin, out):
    limit_raw = _read(stdin, out, "How many ranked results (1-100, default 20): ").strip()
    if limit_raw == "":
        limit = 20
    else:
        try:
            limit = int(limit_raw)
        except ValueError:
            raise ValueError("Invalid limit.")
    results = opportunities.ranked(token, limit=limit)
    if not results:
        out.write("No opportunities to rank.\n")
    else:
        out.write("Ranked (budget desc, risk low->high, deadline asc) — draft heuristic, unverified:\n")
        for item in results:
            out.write(f"{item['id']} | {item['title'][:40]} | INR {item['budget_paise'] // 100}.{item['budget_paise'] % 100:02d} | {item['risk_level']} | deadline:{item['deadline_at']} | {item['status']}\n")
