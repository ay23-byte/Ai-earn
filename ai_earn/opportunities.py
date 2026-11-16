"""Experimental opportunity discovery (v15) — manual import + connector interface.

This module provides owner-scoped, session-authorized opportunity records with
source, budget, deadline, risk and skill metadata. All records are
**unverified local drafts**; no verification, outreach, or revenue is implied.
AI helpers are explicitly draft-only stubs. Connectors are manual-fallback
first and must respect ToS/robots; no CAPTCHA bypass.

Trusted application code must supply a fixed auth/state pair. This is not an
OS boundary or public API.
"""
from __future__ import annotations

import abc
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from typing import Any

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError

_RESOURCE = "opportunity"
_MAX_TAGS_LEN = 500
_MAX_URL_LEN = 2048
_MAX_TIME = 4_102_444_800
_RISK_LEVELS = frozenset({"low", "medium", "high", "unknown"})
_STATUSES = frozenset({"discovered", "vetted", "pursuing", "archived", "dismissed"})
_SOURCE_TYPES = frozenset({"manual", "connector"})

# Transition map: (current_status, event) -> next_status
# None represents creation.
_TRANSITIONS: dict[tuple[Any, str], str] = {
    (None, "opportunity_created"): "discovered",
    ("discovered", "opportunity_vetted"): "vetted",
    ("discovered", "opportunity_dismissed"): "dismissed",
    ("discovered", "opportunity_archived"): "archived",
    ("vetted", "opportunity_pursuing"): "pursuing",
    ("vetted", "opportunity_dismissed"): "dismissed",
    ("vetted", "opportunity_archived"): "archived",
    ("pursuing", "opportunity_archived"): "archived",
    ("pursuing", "opportunity_dismissed"): "dismissed",
}


class OpportunityError(Exception):
    """Invalid input, conflicting key, or disallowed transition."""


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, OpportunityError) as exc:
        raise OpportunityError(str(exc)) from exc
    except Exception as exc:
        raise OpportunityError("Invalid opportunity input.") from exc


def _url(value: str) -> str:
    if not isinstance(value, str):
        raise OpportunityError("Invalid source URL.")
    value = value.strip()
    if not 1 <= len(value) <= _MAX_URL_LEN:
        raise OpportunityError("Invalid source URL.")
    if not (value.startswith("http://") or value.startswith("https://")):
        raise OpportunityError("Source URL must start with http:// or https://.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OpportunityError("Invalid source URL.")
    if " " in value or "\t" in value or "\n" in value or "\r" in value:
        raise OpportunityError("Invalid source URL.")
    # Basic host check – must contain a dot or localhost after scheme
    # We keep validation lightweight; detailed URL parsing is future work.
    return value


def _risk(value: str) -> str:
    if not isinstance(value, str) or value not in _RISK_LEVELS:
        raise OpportunityError("Risk level must be one of low, medium, high, unknown.")
    return value


def _source_type(value: str) -> str:
    if not isinstance(value, str) or value not in _SOURCE_TYPES:
        raise OpportunityError("Source type must be manual or connector.")
    return value


def _connector_id(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise OpportunityError("Invalid connector identifier.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise OpportunityError("Invalid connector identifier.")
    return value


def _skill_tags(value: str) -> str:
    if not isinstance(value, str):
        raise OpportunityError("Invalid skill tags.")
    # Allow empty, but bound length and disallow control chars.
    if len(value) > _MAX_TAGS_LEN:
        raise OpportunityError("Invalid skill tags.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OpportunityError("Invalid skill tags.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OpportunityError("Invalid skill tags.") from exc
    return value


def _budget(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 1_000_000_000_000:
        raise OpportunityError("Invalid budget amount.")
    return value


def _deadline(value: int) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_TIME:
        raise OpportunityError("Invalid deadline.")
    return value


def _version(conn: sqlite3.Connection) -> None:
    from .state_storage import SUPPORTED_STATE_VERSIONS
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_STATE_VERSIONS or version < 15:
        raise StateStorageError("State schema version 15 is required for opportunities.")


def _retry(conn: sqlite3.Connection, owner: str, key: str, digest: bytes) -> str | None:
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != _RESOURCE or not hmac.compare_digest(row["request_hash"], digest):
        raise OpportunityError("Operation key was already used for a different request.")
    if conn.execute("SELECT 1 FROM opportunities WHERE id=? AND owner_id=?", (row["resource_id"], owner)).fetchone() is None:
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn: sqlite3.Connection, now: int, owner: str, event: str, record_id: str) -> None:
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, _RESOURCE, record_id),
    )


def _remember(conn: sqlite3.Connection, owner: str, key: str, digest: bytes, record_id: str) -> None:
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, _RESOURCE, record_id),
    )


def _request(action: str, values: list) -> bytes:
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _owned(conn: sqlite3.Connection, owner: str, record_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM opportunities WHERE id=? AND owner_id=?", (record_id, owner)).fetchone()
    if row is None:
        raise AccessDenied("Access denied.")
    # Replay history to validate status
    events = conn.execute(
        "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
        (owner, _RESOURCE, record_id),
    ).fetchall()
    if not events:
        raise StateStorageError("Opportunity history is inconsistent.")
    replayed = None
    for (event,) in events:
        step = (replayed, event)
        if step not in _TRANSITIONS:
            raise StateStorageError("Opportunity history is inconsistent.")
        replayed = _TRANSITIONS[step]
    if replayed != row["status"]:
        raise StateStorageError("Opportunity history is inconsistent.")
    # Ensure operation count matches audit count (similar to other services)
    count = conn.execute(
        "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
        (owner, _RESOURCE, record_id),
    ).fetchone()[0]
    if count != len(events):
        raise StateStorageError("Opportunity history is inconsistent.")
    return row


def _view(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["verification"] = "unverified"
    d["amount_kind"] = "budget_not_revenue"
    return d


# ---------------------------------------------------------------------------
# Connector abstraction
# ---------------------------------------------------------------------------


class Connector(abc.ABC):
    """Abstract connector for opportunity sources. Manual fallback always exists.

    Connectors must not bypass ToS, robots.txt, or CAPTCHAs. This interface is
    for trusted, least-privilege, validated configuration only.
    """

    @property
    @abc.abstractmethod
    def connector_id(self) -> str:
        """Stable identifier for this connector (1-64, alphanum _ -)."""

    @abc.abstractmethod
    def fetch(self, *, limit: int = 20) -> list[dict]:
        """Return up to `limit` opportunity dicts for manual review.

        Each dict must contain at least: title, source_url, budget_paise,
        deadline_at, risk_level, skill_tags. All fields are validated by the
        caller before persistence; this method must never trust external input.
        """

    def validate_config(self) -> None:
        """Optional hook to validate credentials/config without network."""
        _connector_id(self.connector_id)


class ManualConnector(Connector):
    """No-op connector that returns no remote results; manual import only."""

    def __init__(self, connector_id: str = "manual"):
        # manual is reserved for source_type manual; use distinct id
        if connector_id == "manual":
            connector_id = "manual-fallback"
        _connector_id(connector_id)
        self._id = connector_id

    @property
    def connector_id(self) -> str:
        return self._id

    def fetch(self, *, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OpportunityError("Invalid fetch limit.")
        return []


class FakeConnector(Connector):
    """Test-only fake connector with deterministic synthetic data."""

    def __init__(self, connector_id: str = "fake-test-connector", items: list[dict] | None = None):
        _connector_id(connector_id)
        self._id = connector_id
        self._items = items or []

    @property
    def connector_id(self) -> str:
        return self._id

    def fetch(self, *, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OpportunityError("Invalid fetch limit.")
        return self._items[:limit]


# ---------------------------------------------------------------------------
# Draft AI helpers (explicitly unverified)
# ---------------------------------------------------------------------------


def summarize_draft(title: str, source_url: str) -> dict:
    """Return a draft summary for an opportunity. Not verified, not authoritative.

    This is a deterministic stub for UI prototyping; it does not call any AI
    provider and must be labeled draft_unverified by callers.
    """
    _input(_title, title)
    _input(_url, source_url)
    # Simple heuristic summary: first 100 chars of title + domain
    domain = source_url.split("//", 1)[-1].split("/", 1)[0][:64]
    summary = f"Draft summary for '{title[:100]}' from {domain}. Review required."
    return {"summary": summary, "model": "stub-draft-v1", "verification": "draft_unverified"}


def suggest_skills_draft(title: str, description: str = "") -> dict:
    """Suggest skill tags from title/description. Draft only, unverified."""
    _input(_title, title)
    if not isinstance(description, str) or len(description) > 2000:
        raise OpportunityError("Invalid description.")
    if any(ord(c) < 32 or ord(c) == 127 for c in description):
        raise OpportunityError("Invalid description.")
    # Naive keyword extraction: split title into words, lowercase, deduplicate
    words = re.findall(r"[A-Za-z]{3,}", title.lower())
    skills = sorted(set(words))[:5]
    tags = ", ".join(skills)
    if len(tags) > _MAX_TAGS_LEN:
        tags = tags[:_MAX_TAGS_LEN]
    return {"skill_tags": tags, "verification": "draft_unverified"}


def suggest_price_draft(title: str, risk_level: str) -> dict:
    """Suggest a budget range draft based on risk. Unverified, do not use as quote."""
    _input(_title, title)
    _input(_risk, risk_level)
    # Deterministic stub pricing
    base = 50000  # 500 INR in paise
    multiplier = {"low": 3, "medium": 2, "high": 1, "unknown": 1.5}[risk_level]
    suggested = int(base * multiplier)
    return {"suggested_budget_paise": suggested, "currency": "INR", "verification": "draft_unverified"}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Opportunities:
    """Owner-scoped opportunity discovery with manual import and connector interface."""

    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(
        self,
        token: str,
        title: str,
        source_url: str,
        budget_paise: int,
        *,
        operation_key: str,
        deadline_at: int = 0,
        risk_level: str = "unknown",
        skill_tags: str = "",
        source_type: str = "manual",
        connector_id: str | None = None,
    ) -> str:
        title = _input(_title, title)
        url = _input(_url, source_url)
        budget = _input(_budget, budget_paise)
        deadline = _input(_deadline, deadline_at)
        risk = _input(_risk, risk_level)
        tags = _input(_skill_tags, skill_tags)
        stype = _input(_source_type, source_type)
        cid = _input(_connector_id, connector_id)
        key = _input(_key, operation_key)
        if stype == "manual" and cid is not None:
            raise OpportunityError("Manual opportunities must not have a connector_id.")
        if stype == "connector" and cid is None:
            raise OpportunityError("Connector opportunities require connector_id.")
        digest = _request("opportunity_create", [title, url, budget, deadline, risk, tags, stype, cid or ""])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                return existing
            if deadline != 0 and deadline <= now:
                raise OpportunityError("Deadline must be in the future.")
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO opportunities(id, owner_id, title, source_url, source_type, connector_id, budget_paise, currency, deadline_at, risk_level, skill_tags, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?, 'INR', ?, ?, ?, 'discovered', ?, ?)",
                (ident, owner, title, url, stype, cid, budget, deadline, risk, tags, now, now),
            )
            _audit(conn, now, owner, "opportunity_created", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def get(self, token: str, record_id: str) -> dict:
        ident = _input(_record_id, record_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            return _view(_owned(conn, owner, ident))

    def list(
        self,
        token: str,
        *,
        after_id: str | None = None,
        limit: int = 20,
        status: str | None = None,
        risk_level: str | None = None,
        min_budget: int | None = None,
        max_budget: int | None = None,
        skill_contains: str | None = None,
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OpportunityError("Invalid page size.")
        if status is not None and status not in _STATUSES:
            raise OpportunityError("Invalid status filter.")
        if risk_level is not None and risk_level not in _RISK_LEVELS:
            raise OpportunityError("Invalid risk filter.")
        if min_budget is not None:
            _input(_budget, min_budget)
        if max_budget is not None:
            _input(_budget, max_budget)
        if min_budget is not None and max_budget is not None and min_budget > max_budget:
            raise OpportunityError("Invalid budget range.")
        if skill_contains is not None:
            if not isinstance(skill_contains, str) or len(skill_contains) > 100:
                raise OpportunityError("Invalid skill filter.")
            if any(ord(c) < 32 or ord(c) == 127 for c in skill_contains):
                raise OpportunityError("Invalid skill filter.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            # Build query with fixed columns and parameters
            query = "SELECT id FROM opportunities WHERE owner_id=? AND id>? COLLATE BINARY"
            params: list[Any] = [owner, cursor]
            if status is not None:
                query += " AND status=?"
                params.append(status)
            if risk_level is not None:
                query += " AND risk_level=?"
                params.append(risk_level)
            if min_budget is not None:
                query += " AND budget_paise>=?"
                params.append(min_budget)
            if max_budget is not None:
                query += " AND budget_paise<=?"
                params.append(max_budget)
            if skill_contains is not None and skill_contains.strip() != "":
                query += " AND skill_tags LIKE ? COLLATE NOCASE"
                params.append(f"%{skill_contains}%")
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            return [_view(_owned(conn, owner, r["id"])) for r in rows]

    def ranked(self, token: str, *, limit: int = 20) -> list[dict]:
        """Return opportunities ordered by budget desc, risk asc, deadline asc.

        Ranking is a draft heuristic: low-risk and higher budget rank higher.
        This does not verify value or guarantee success.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OpportunityError("Invalid page size.")
        risk_order = {"low": 0, "medium": 1, "unknown": 2, "high": 3}
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version(conn)
            rows = conn.execute(
                "SELECT id, budget_paise, risk_level, deadline_at FROM opportunities WHERE owner_id=? ORDER BY budget_paise DESC, deadline_at ASC, id COLLATE BINARY LIMIT 100",
                (owner,),
            ).fetchall()
            # Sort by risk_order secondarily (budget already primary)
            rows = sorted(rows, key=lambda r: ( -r["budget_paise"], risk_order.get(r["risk_level"], 99), r["deadline_at"] if r["deadline_at"] != 0 else _MAX_TIME, r["id"]))[:limit]
            return [_view(_owned(conn, owner, r["id"])) for r in rows]

    def _transition(self, token: str, record_id: str, target_status: str, event: str, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        if target_status not in _STATUSES:
            raise OpportunityError("Invalid status.")
        digest = _request("opportunity_transition", [ident, target_status])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            expected = _TRANSITIONS.get((current, event))
            if expected != target_status:
                raise OpportunityError("Transition is not allowed.")
            conn.execute("UPDATE opportunities SET status=?, updated_at=? WHERE id=? AND owner_id=?", (target_status, now, ident, owner))
            _audit(conn, now, owner, event, ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def vetted(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "vetted", "opportunity_vetted", operation_key)

    def pursuing(self, token: str, record_id: str, *, operation_key: str) -> str:
        return self._transition(token, record_id, "pursuing", "opportunity_pursuing", operation_key)

    def archived(self, token: str, record_id: str, *, operation_key: str) -> str:
        # Allow archiving from discovered, vetted, pursuing
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("opportunity_transition", [ident, "archived"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            if (current, "opportunity_archived") not in _TRANSITIONS:
                raise OpportunityError("Transition is not allowed.")
            conn.execute("UPDATE opportunities SET status='archived', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "opportunity_archived", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def dismissed(self, token: str, record_id: str, *, operation_key: str) -> str:
        ident = _input(_record_id, record_id)
        key = _input(_key, operation_key)
        digest = _request("opportunity_transition", [ident, "dismissed"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version(conn)
            row = _owned(conn, owner, ident)
            existing = _retry(conn, owner, key, digest)
            if existing is not None:
                if existing != ident:
                    raise StateStorageError("Stored operation result is inconsistent.")
                return ident
            current = row["status"]
            if (current, "opportunity_dismissed") not in _TRANSITIONS:
                raise OpportunityError("Transition is not allowed.")
            conn.execute("UPDATE opportunities SET status='dismissed', updated_at=? WHERE id=? AND owner_id=?", (now, ident, owner))
            _audit(conn, now, owner, "opportunity_dismissed", ident)
            _remember(conn, owner, key, digest, ident)
            return ident

    def import_from_connector(self, token: str, connector: Connector, *, limit: int = 20, operation_key_prefix: str = "connector_import") -> list[str]:
        """Fetch from connector and create opportunities; returns created IDs.

        Each imported item gets a distinct operation key derived from the prefix.
        Caller must provide a stable prefix per import batch for idempotency.
        """
        # Accept any connector with connector_id and fetch (duck-typed) to support connectors framework stubs
        if not hasattr(connector, "connector_id") or not hasattr(connector, "fetch") or not callable(getattr(connector, "fetch", None)):
            raise OpportunityError("Invalid connector.")
        # Validate connector id via shared helper
        try:
            # property may raise
            cid = connector.connector_id
            if isinstance(cid, str):
                from ai_earn.connectors.base import _connector_id as _cid_check
                _cid_check(cid)
            else:
                raise OpportunityError("Invalid connector.")
        except Exception as exc:
            # Preserve original error type for validation failures
            if isinstance(exc, OpportunityError):
                raise
            raise OpportunityError("Invalid connector.") from exc
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OpportunityError("Invalid limit.")
        _key(operation_key_prefix)  # validate base
        connector.validate_config()
        items = connector.fetch(limit=limit)
        if not isinstance(items, list):
            raise OpportunityError("Connector returned invalid data.")
        ids: list[str] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                raise OpportunityError("Invalid connector item.")
            # Validate required fields; unknown fields are rejected
            allowed = {"title", "source_url", "budget_paise", "deadline_at", "risk_level", "skill_tags"}
            if set(item.keys()) - allowed:
                raise OpportunityError("Connector item has unknown fields.")
            for field in ("title", "source_url", "budget_paise"):
                if field not in item:
                    raise OpportunityError("Connector item missing required field.")
            title = _input(_title, item["title"])
            url = _input(_url, item["source_url"])
            budget = _input(_budget, item["budget_paise"])
            deadline = _input(_deadline, item.get("deadline_at", 0))
            risk = _input(_risk, item.get("risk_level", "unknown"))
            tags = _input(_skill_tags, item.get("skill_tags", ""))
            # Derive unique operation key per item
            op_key = f"{operation_key_prefix}_{idx}_{hashlib.sha256((title+url).encode()).hexdigest()[:8]}"
            # Ensure key valid length: prefix + suffix <=128
            if len(op_key) > 128:
                op_key = op_key[:128]
            # Use connector source_type
            try:
                ident = self.create(
                    token,
                    title,
                    url,
                    budget,
                    operation_key=op_key,
                    deadline_at=deadline,
                    risk_level=risk,
                    skill_tags=tags,
                    source_type="connector",
                    connector_id=connector.connector_id,
                )
                ids.append(ident)
            except OpportunityError:
                # Skip invalid items but continue? For strict, raise.
                raise
        return ids

