"""Normalize external items to opportunity fields — bounded, validated."""

from .base import ConnectorError

def _title(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise ConnectorError("Invalid title.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConnectorError("Invalid title.")
    return value

def _url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ConnectorError("Invalid URL.")
    if not (value.startswith("https://") or value.startswith("http://")):
        raise ConnectorError("Invalid URL.")
    if any(c.isspace() or ord(c) < 32 or ord(c)==127 for c in value):
        raise ConnectorError("Invalid URL.")
    return value

def _budget(value: int) -> int:
    if not isinstance(value, int) or not 0 <= value <= 1_000_000_000_000:
        raise ConnectorError("Invalid budget.")
    return value

def normalize_gmail(raw: dict) -> dict:
    # Expected raw: {id, subject, snippet, link, budget_paise?, deadline_at?, risk_level?, skill_tags?}
    if not isinstance(raw, dict):
        raise ConnectorError("Invalid Gmail item.")
    allowed = {"id", "subject", "snippet", "link", "budget_paise", "deadline_at", "risk_level", "skill_tags"}
    if not set(raw).issubset(allowed):
        raise ConnectorError("Extra field in Gmail item.")
    if "subject" not in raw or "link" not in raw:
        raise ConnectorError("Missing Gmail field.")
    title = _title(raw["subject"])
    url = _url(raw["link"])
    budget = _budget(raw.get("budget_paise", 0))
    deadline = raw.get("deadline_at", 0)
    if not isinstance(deadline, int) or not 0 <= deadline <= 4102444800:
        raise ConnectorError("Invalid deadline.")
    risk = raw.get("risk_level", "unknown")
    if risk not in {"low","medium","high","unknown"}:
        raise ConnectorError("Invalid risk.")
    tags = raw.get("skill_tags", "")
    if not isinstance(tags, str) or len(tags) > 500:
        raise ConnectorError("Invalid skill_tags.")
    if any(ord(c) < 32 or ord(c)==127 for c in tags):
        raise ConnectorError("Invalid skill_tags.")
    snippet = raw.get("snippet", "")
    if not isinstance(snippet, str) or len(snippet) > 2000:
        raise ConnectorError("Invalid snippet.")
    return {
        "title": title,
        "source_url": url,
        "budget_paise": budget,
        "deadline_at": deadline,
        "risk_level": risk,
        "skill_tags": tags,
    }

def normalize_drive(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConnectorError("Invalid Drive item.")
    allowed = {"id", "name", "webViewLink", "budget_paise", "deadline_at", "risk_level", "skill_tags"}
    if not set(raw).issubset(allowed):
        raise ConnectorError("Extra field in Drive item.")
    if "name" not in raw or "webViewLink" not in raw:
        raise ConnectorError("Missing Drive field.")
    title = _title(raw["name"])
    url = _url(raw["webViewLink"])
    budget = _budget(raw.get("budget_paise", 0))
    deadline = raw.get("deadline_at", 0)
    if not isinstance(deadline, int) or not 0 <= deadline <= 4102444800:
        raise ConnectorError("Invalid deadline.")
    risk = raw.get("risk_level", "unknown")
    if risk not in {"low","medium","high","unknown"}:
        raise ConnectorError("Invalid risk.")
    tags = raw.get("skill_tags", "")
    if not isinstance(tags, str) or len(tags) > 500:
        raise ConnectorError("Invalid skill_tags.")
    if any(ord(c) < 32 or ord(c)==127 for c in tags):
        raise ConnectorError("Invalid skill_tags.")
    return {
        "title": title,
        "source_url": url,
        "budget_paise": budget,
        "deadline_at": deadline,
        "risk_level": risk,
        "skill_tags": tags,
    }

def normalize_github(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConnectorError("Invalid GitHub item.")
    allowed = {"id", "title", "html_url", "budget_paise", "deadline_at", "risk_level", "skill_tags", "labels"}
    if not set(raw).issubset(allowed):
        raise ConnectorError("Extra field in GitHub item.")
    if "title" not in raw or "html_url" not in raw:
        raise ConnectorError("Missing GitHub field.")
    title = _title(raw["title"])
    url = _url(raw["html_url"])
    budget = _budget(raw.get("budget_paise", 0))
    deadline = raw.get("deadline_at", 0)
    if not isinstance(deadline, int) or not 0 <= deadline <= 4102444800:
        raise ConnectorError("Invalid deadline.")
    risk = raw.get("risk_level", "unknown")
    if risk not in {"low","medium","high","unknown"}:
        raise ConnectorError("Invalid risk.")
    tags = raw.get("skill_tags", "")
    if not isinstance(tags, str) or len(tags) > 500:
        raise ConnectorError("Invalid skill_tags.")
    if any(ord(c) < 32 or ord(c)==127 for c in tags):
        raise ConnectorError("Invalid skill_tags.")
    # labels ignored except for validation
    labels = raw.get("labels", [])
    if not isinstance(labels, list) or any(not isinstance(x, str) or len(x)>100 for x in labels):
        raise ConnectorError("Invalid labels.")
    return {
        "title": title,
        "source_url": url,
        "budget_paise": budget,
        "deadline_at": deadline,
        "risk_level": risk,
        "skill_tags": tags,
    }
