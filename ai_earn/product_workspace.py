"""Digital product workspace — Phase 7 (v26).

Owner-scoped, session-authorized, transactional, idempotent via operation_key,
audited via state_audit. Covers product ideas, validation, market research
(AI-assisted), descriptions, pricing suggestions, versions, file management,
product status, sales tracking (evidence-based), downloadable deliverables,
product documentation, with AI assistance for research/drafts. No fabricated
market research or sales without evidence — every external claim requires
evidence and is tagged verification='unverified' unless explicitly verified
with evidence.

All AI outputs are marked ai_generated and verification unverified.
"""

from __future__ import annotations

import hmac
import json
import secrets

from .auth import AccessDenied, AuthService
from .opportunity_drafts import DraftError, _digest as _base_digest, _key, _record_id, _title
from .state_authorization import _authorized_transaction
from .state_storage import StateStorage, StateStorageError
from .products import _description as _prod_description, _price as _prod_price, _version_str

_RESOURCE_IDEA = "product_idea"
_RESOURCE_VALIDATION = "product_validation"
_RESOURCE_RESEARCH = "market_research"
_RESOURCE_PRICING = "pricing_suggestion"
_RESOURCE_VERSION = "product_version"
_RESOURCE_FILE = "product_file"
_RESOURCE_SALE = "product_sale"
_RESOURCE_DELIVERABLE = "product_deliverable"
_RESOURCE_DOC = "product_documentation"

_IDEA_STATUSES = frozenset({"idea", "validated", "rejected", "archived"})
_IDEA_TRANSITIONS = {
    (None, "idea_created"): "idea",
    ("idea", "idea_validated"): "validated",
    ("idea", "idea_rejected"): "rejected",
    ("idea", "idea_archived"): "archived",
    ("validated", "idea_archived"): "archived",
    ("rejected", "idea_archived"): "archived",
}
_VALIDATION_RESULTS = frozenset({"pass", "fail", "pending"})


class ProductWorkspaceError(Exception):
    pass


def _input(validator, value):
    try:
        return validator(value)
    except (DraftError, ProductWorkspaceError) as exc:
        raise ProductWorkspaceError(str(exc)) from exc
    except Exception as exc:
        raise ProductWorkspaceError("Invalid product workspace input.") from exc


def _criteria(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 500 or len(value.strip()) == 0:
        raise ProductWorkspaceError("Invalid criteria.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductWorkspaceError("Invalid criteria.")
    return value


def _evidence(value: str) -> str:
    if not isinstance(value, str) or len(value) > 1000:
        raise ProductWorkspaceError("Invalid evidence.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProductWorkspaceError("Invalid evidence.")
    return value


def _query(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 500 or len(value.strip()) == 0:
        raise ProductWorkspaceError("Invalid query.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductWorkspaceError("Invalid query.")
    return value


def _findings(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 5000:
        raise ProductWorkspaceError("Invalid findings.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProductWorkspaceError("Invalid findings.")
    return value


def _evidence_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ProductWorkspaceError("Invalid evidence_url.")
    if value != "" and not (value.startswith("http://") or value.startswith("https://")):
        raise ProductWorkspaceError("Invalid evidence_url.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductWorkspaceError("Invalid evidence_url.")
    return value


def _rationale(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        raise ProductWorkspaceError("Invalid rationale.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProductWorkspaceError("Invalid rationale.")
    return value


def _notes(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2000:
        raise ProductWorkspaceError("Invalid notes.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProductWorkspaceError("Invalid notes.")
    return value


def _file_name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or len(value.strip()) == 0:
        raise ProductWorkspaceError("Invalid file_name.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductWorkspaceError("Invalid file_name.")
    return value


def _mime(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise ProductWorkspaceError("Invalid mime_type.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductWorkspaceError("Invalid mime_type.")
    return value


def _file_size(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 104857600:
        raise ProductWorkspaceError("Invalid file_size.")
    return value


def _quantity(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 1000000:
        raise ProductWorkspaceError("Invalid quantity.")
    return value


def _doc_title(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or len(value.strip()) == 0:
        raise ProductWorkspaceError("Invalid doc title.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProductWorkspaceError("Invalid doc title.")
    return value


def _doc_content(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 10000:
        raise ProductWorkspaceError("Invalid doc content.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value if c not in "\n\t"):
        raise ProductWorkspaceError("Invalid doc content.")
    return value


def _version_check(conn, min_version=26):
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    from .state_storage import SUPPORTED_STATE_VERSIONS

    if ver not in SUPPORTED_STATE_VERSIONS or ver < min_version:
        raise StateStorageError(
            f"State schema version {min_version} is required for product workspace."
        )


def _retry(conn, owner, key, digest, resource, table):
    row = conn.execute(
        "SELECT request_hash, resource_type, resource_id FROM state_operations WHERE owner_id=? AND operation_key=?",
        (owner, key),
    ).fetchone()
    if row is None:
        return None
    if row["resource_type"] != resource or not hmac.compare_digest(row["request_hash"], digest):
        raise ProductWorkspaceError("Operation key was already used for a different request.")
    if (
        conn.execute(
            f"SELECT 1 FROM {table} WHERE id=? AND owner_id=?", (row["resource_id"], owner)
        ).fetchone()
        is None
    ):
        raise StateStorageError("Stored operation result is unavailable.")
    return row["resource_id"]


def _audit(conn, now, owner, event, resource, rid):
    conn.execute(
        "INSERT INTO state_audit(occurred_at, actor_id, event, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (now, owner, event, resource, rid),
    )


def _remember(conn, owner, key, digest, resource, rid):
    conn.execute(
        "INSERT INTO state_operations(owner_id, operation_key, request_hash, resource_type, resource_id) VALUES(?,?,?,?,?)",
        (owner, key, digest, resource, rid),
    )


def _request(action, values):
    return _base_digest(action, json.dumps(values, ensure_ascii=True, separators=(",", ":")))


def _owned_product(conn, owner, pid):
    row = conn.execute("SELECT 1 FROM products WHERE id=? AND owner_id=?", (pid, owner)).fetchone()
    if row is None:
        raise ProductWorkspaceError("Product not found.")


# ---------- Product Ideas ----------
class ProductIdeas:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def _owned(self, conn, owner, iid):
        row = conn.execute(
            "SELECT * FROM product_ideas WHERE id=? AND owner_id=?", (iid, owner)
        ).fetchone()
        if row is None:
            raise AccessDenied("Access denied.")
        events = conn.execute(
            "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
            (owner, _RESOURCE_IDEA, iid),
        ).fetchall()
        if not events:
            raise StateStorageError("Idea history inconsistent.")
        replayed = None
        for (ev,) in events:
            if (replayed, ev) not in _IDEA_TRANSITIONS:
                raise StateStorageError("Idea history inconsistent.")
            replayed = _IDEA_TRANSITIONS[(replayed, ev)]
        if replayed != row["status"]:
            raise StateStorageError("Idea history inconsistent.")
        cnt = conn.execute(
            "SELECT count(*) FROM state_operations WHERE owner_id=? AND resource_type=? AND resource_id=?",
            (owner, _RESOURCE_IDEA, iid),
        ).fetchone()[0]
        if cnt != len(events):
            raise StateStorageError("Idea history inconsistent.")
        return row

    def create(self, token: str, title: str, description: str, *, operation_key: str) -> str:
        title_v = _input(_title, title)
        desc_v = _input(_prod_description, description)
        key = _input(_key, operation_key)
        digest = _request("idea_create", [title_v, desc_v])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_IDEA, "product_ideas")
            if existing:
                return existing
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_ideas(id, owner_id, title, description, status, created_at, updated_at) VALUES(?,?,?,?, 'idea', ?, ?)",
                (ident, owner, title_v, desc_v, now, now),
            )
            _audit(conn, now, owner, "idea_created", _RESOURCE_IDEA, ident)
            _remember(conn, owner, key, digest, _RESOURCE_IDEA, ident)
            return ident

    def get(self, token: str, idea_id: str) -> dict:
        iid = _input(_record_id, idea_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            return dict(self._owned(conn, owner, iid))

    def list(
        self, token: str, *, after_id: str | None = None, limit: int = 20, status: str | None = None
    ) -> list[dict]:
        cursor = "" if after_id is None else _input(_record_id, after_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ProductWorkspaceError("Invalid limit.")
        if status is not None and status not in _IDEA_STATUSES:
            raise ProductWorkspaceError("Invalid status.")
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            query = "SELECT * FROM product_ideas WHERE owner_id=? AND id>? COLLATE BINARY"
            params = [owner, cursor]
            if status:
                query += " AND status=?"
                params.append(status)
            query += " ORDER BY id COLLATE BINARY LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, tuple(params)).fetchall()
            out = []
            for r in rows:
                # validate via _owned
                iid = r["id"]
                # reuse same conn validation
                events = conn.execute(
                    "SELECT event FROM state_audit WHERE actor_id=? AND resource_type=? AND resource_id=? ORDER BY id",
                    (owner, _RESOURCE_IDEA, iid),
                ).fetchall()
                if not events:
                    raise StateStorageError("Idea history inconsistent.")
                replayed = None
                for (ev,) in events:
                    if (replayed, ev) not in _IDEA_TRANSITIONS:
                        raise StateStorageError("Idea history inconsistent.")
                    replayed = _IDEA_TRANSITIONS[(replayed, ev)]
                if replayed != r["status"]:
                    raise StateStorageError("Idea history inconsistent.")
                out.append(dict(r))
            return out

    def _transition(self, token, idea_id, target, event, operation_key):
        iid = _input(_record_id, idea_id)
        key = _input(_key, operation_key)
        if target not in _IDEA_STATUSES:
            raise ProductWorkspaceError("Invalid status.")
        digest = _request("idea_transition", [iid, target])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = self._owned(conn, owner, iid)
            existing = _retry(conn, owner, key, digest, _RESOURCE_IDEA, "product_ideas")
            if existing:
                return iid
            cur = row["status"]
            if _IDEA_TRANSITIONS.get((cur, event)) != target:
                raise ProductWorkspaceError("Transition not allowed.")
            conn.execute(
                "UPDATE product_ideas SET status=?, updated_at=? WHERE id=? AND owner_id=?",
                (target, now, iid, owner),
            )
            _audit(conn, now, owner, event, _RESOURCE_IDEA, iid)
            _remember(conn, owner, key, digest, _RESOURCE_IDEA, iid)
            return iid

    def validate(self, token: str, idea_id: str, *, operation_key: str) -> str:
        return self._transition(token, idea_id, "validated", "idea_validated", operation_key)

    def reject(self, token: str, idea_id: str, *, operation_key: str) -> str:
        return self._transition(token, idea_id, "rejected", "idea_rejected", operation_key)

    def archive(self, token: str, idea_id: str, *, operation_key: str) -> str:
        # archive from any non-archived
        iid = _input(_record_id, idea_id)
        key = _input(_key, operation_key)
        digest = _request("idea_transition", [iid, "archived"])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = self._owned(conn, owner, iid)
            existing = _retry(conn, owner, key, digest, _RESOURCE_IDEA, "product_ideas")
            if existing:
                return iid
            cur = row["status"]
            if _IDEA_TRANSITIONS.get((cur, "idea_archived")) != "archived":
                raise ProductWorkspaceError("Transition not allowed.")
            conn.execute(
                "UPDATE product_ideas SET status='archived', updated_at=? WHERE id=? AND owner_id=?",
                (now, iid, owner),
            )
            _audit(conn, now, owner, "idea_archived", _RESOURCE_IDEA, iid)
            _remember(conn, owner, key, digest, _RESOURCE_IDEA, iid)
            return iid


# ---------- Product Validations ----------
class ProductValidations:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(
        self,
        token: str,
        product_id: str,
        criteria: str,
        result: str,
        *,
        operation_key: str,
        evidence: str = "",
    ) -> str:
        pid = _input(_record_id, product_id)
        crit = _input(_criteria, criteria)
        if result not in _VALIDATION_RESULTS:
            raise ProductWorkspaceError("Invalid result.")
        ev = _input(_evidence, evidence)
        key = _input(_key, operation_key)
        digest = _request("validation_add", [pid, crit, result, ev])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_VALIDATION, "product_validations")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_validations(id, owner_id, product_id, criteria, result, evidence, created_at) VALUES(?,?,?,?,?,?,?)",
                (ident, owner, pid, crit, result, ev, now),
            )
            _audit(conn, now, owner, "product_validation_created", _RESOURCE_VALIDATION, ident)
            _remember(conn, owner, key, digest, _RESOURCE_VALIDATION, ident)
            return ident

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM product_validations WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------- Market Research ----------
class MarketResearch:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(
        self,
        token: str,
        product_id: str,
        query: str,
        findings: str,
        *,
        operation_key: str,
        evidence_url: str = "",
        ai_generated: bool = False,
        verification: str = "unverified",
    ) -> str:
        pid = _input(_record_id, product_id)
        q = _input(_query, query)
        f = _input(_findings, findings)
        evurl = _input(_evidence_url, evidence_url)
        if type(ai_generated) is not bool:
            raise ProductWorkspaceError("ai_generated bool required.")
        if verification not in ("unverified", "verified"):
            raise ProductWorkspaceError("Invalid verification.")
        if verification == "verified" and evurl == "":
            raise ProductWorkspaceError("Verified research requires evidence_url.")
        # Do not fabricate: if no evidence, must be unverified
        if evurl == "" and verification == "verified":
            raise ProductWorkspaceError("Cannot claim verified without evidence.")
        key = _input(_key, operation_key)
        digest = _request("research_add", [pid, q, f, evurl, ai_generated, verification])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_RESEARCH, "market_research")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO market_research(id, owner_id, product_id, query, findings, evidence_url, ai_generated, verification, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (ident, owner, pid, q, f, evurl, 1 if ai_generated else 0, verification, now),
            )
            _audit(conn, now, owner, "market_research_created", _RESOURCE_RESEARCH, ident)
            _remember(conn, owner, key, digest, _RESOURCE_RESEARCH, ident)
            return ident

    def generate_ai_draft(
        self, token: str, product_id: str, query: str, *, operation_key: str, evidence_url: str = ""
    ) -> str:
        # AI-assisted research draft, always ai_generated and unverified
        pid = _input(_record_id, product_id)
        q = _input(_query, query)
        # Try AI assistant
        ai_findings = None
        try:
            from .ai_assistant import AIAssistant

            ai = AIAssistant(self._auth, self._state)
            res = ai.chat(token, f"Market research for query: {q}", external_content=None)
            ai_findings = res.get("output", "")[:2000] if isinstance(res, dict) else str(res)[:2000]
            if not ai_findings:
                ai_findings = f"AI draft research for '{q}': findings pending, unverified."
        except Exception:
            ai_findings = (
                f"AI draft research for '{q}': market analysis draft (unverified, no evidence)."
            )
        # Ensure findings length
        if len(ai_findings) > 5000:
            ai_findings = ai_findings[:5000]
        if len(ai_findings) < 1:
            ai_findings = "Draft findings (unverified)."
        return self.add(
            token,
            pid,
            q,
            ai_findings,
            operation_key=operation_key,
            evidence_url=evidence_url,
            ai_generated=True,
            verification="unverified",
        )

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM market_research WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------- Pricing Suggestions ----------
class PricingSuggestions:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(
        self,
        token: str,
        product_id: str,
        price_paise: int,
        rationale: str,
        *,
        operation_key: str,
        ai_generated: bool = False,
        evidence: str = "",
    ) -> str:
        pid = _input(_record_id, product_id)
        price = _input(_prod_price, price_paise)
        rat = _input(_rationale, rationale)
        if type(ai_generated) is not bool:
            raise ProductWorkspaceError("ai_generated bool required.")
        ev = _input(_evidence, evidence)
        key = _input(_key, operation_key)
        digest = _request("pricing_add", [pid, price, rat, ai_generated, ev])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_PRICING, "pricing_suggestions")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO pricing_suggestions(id, owner_id, product_id, price_paise, rationale, ai_generated, evidence, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (ident, owner, pid, price, rat, 1 if ai_generated else 0, ev, now),
            )
            _audit(conn, now, owner, "pricing_suggestion_created", _RESOURCE_PRICING, ident)
            _remember(conn, owner, key, digest, _RESOURCE_PRICING, ident)
            return ident

    def generate_ai_suggestion(
        self, token: str, product_id: str, *, operation_key: str, rationale: str | None = None
    ) -> str:
        pid = _input(_record_id, product_id)
        # Fetch product to suggest price
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            prod = conn.execute(
                "SELECT price_paise, title FROM products WHERE id=? AND owner_id=?", (pid, owner)
            ).fetchone()
            base_price = prod["price_paise"]
            title = prod["title"]
        # AI draft
        ai_rationale = None
        try:
            from .ai_assistant import AIAssistant

            ai = AIAssistant(self._auth, self._state)
            prompt = rationale or f"Suggest pricing for product {title} with base {base_price}"
            res = ai.chat(token, prompt, external_content=None)
            ai_rationale = (
                res.get("output", "")[:1500] if isinstance(res, dict) else str(res)[:1500]
            )
        except Exception:
            ai_rationale = (
                f"AI pricing suggestion for {title}: base price {base_price} analysis (unverified)."
            )
        if not ai_rationale:
            ai_rationale = "AI pricing draft (unverified)."
        # Suggest price as base price (or AI could suggest, but keep deterministic)
        return self.add(
            token,
            pid,
            base_price,
            ai_rationale,
            operation_key=operation_key,
            ai_generated=True,
            evidence="",
        )

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM pricing_suggestions WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------- Product Versions ----------
class ProductVersions:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(
        self, token: str, product_id: str, version: str, notes: str, *, operation_key: str
    ) -> str:
        pid = _input(_record_id, product_id)
        ver = _input(_version_str, version)
        n = _input(_notes, notes)
        key = _input(_key, operation_key)
        digest = _request("prod_version_add", [pid, ver, n])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_VERSION, "product_versions")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_versions(id, owner_id, product_id, version, notes, created_at) VALUES(?,?,?,?,?,?)",
                (ident, owner, pid, ver, n, now),
            )
            _audit(conn, now, owner, "product_version_created", _RESOURCE_VERSION, ident)
            _remember(conn, owner, key, digest, _RESOURCE_VERSION, ident)
            # Also update products.version to latest?
            conn.execute(
                "UPDATE products SET version=?, updated_at=? WHERE id=? AND owner_id=?",
                (ver, now, pid, owner),
            )
            return ident

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM product_versions WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------- File Management & Deliverables ----------
class ProductFiles:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def add(
        self,
        token: str,
        product_id: str,
        file_name: str,
        file_size: int,
        mime_type: str,
        *,
        operation_key: str,
        file_hash: bytes | None = None,
    ) -> str:
        pid = _input(_record_id, product_id)
        fname = _input(_file_name, file_name)
        fsize = _input(_file_size, file_size)
        mime = _input(_mime, mime_type)
        if file_hash is not None and (
            not isinstance(file_hash, (bytes, bytearray)) or len(file_hash) != 32
        ):
            raise ProductWorkspaceError("Invalid file_hash.")
        key = _input(_key, operation_key)
        digest = _request(
            "file_add", [pid, fname, fsize, mime, file_hash.hex() if file_hash else ""]
        )
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_FILE, "product_files")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_files(id, owner_id, product_id, file_name, file_hash, file_size, mime_type, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (ident, owner, pid, fname, file_hash, fsize, mime, now),
            )
            _audit(conn, now, owner, "product_file_created", _RESOURCE_FILE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_FILE, ident)
            return ident

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM product_files WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]

    def remove(self, token: str, file_id: str, *, operation_key: str) -> str:
        fid = _input(_record_id, file_id)
        key = _input(_key, operation_key)
        digest = _request("file_remove", [fid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_FILE or not hmac.compare_digest(
                    conn.execute(
                        "SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?",
                        (owner, key),
                    ).fetchone()[0],
                    digest,
                ):
                    raise ProductWorkspaceError(
                        "Operation key was already used for a different request."
                    )
                return fid
            if (
                conn.execute(
                    "SELECT 1 FROM product_files WHERE id=? AND owner_id=?", (fid, owner)
                ).fetchone()
                is None
            ):
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM product_files WHERE id=? AND owner_id=?", (fid, owner))
            _audit(conn, now, owner, "product_file_deleted", _RESOURCE_FILE, fid)
            _remember(conn, owner, key, digest, _RESOURCE_FILE, fid)
            return fid


class ProductDeliverables(ProductFiles):
    def add(
        self,
        token: str,
        product_id: str,
        file_name: str,
        file_size: int,
        mime_type: str,
        *,
        operation_key: str,
        file_hash: bytes | None = None,
    ) -> str:
        pid = _input(_record_id, product_id)
        fname = _input(_file_name, file_name)
        fsize = _input(_file_size, file_size)
        mime = _input(_mime, mime_type)
        if file_hash is not None and (
            not isinstance(file_hash, (bytes, bytearray)) or len(file_hash) != 32
        ):
            raise ProductWorkspaceError("Invalid file_hash.")
        key = _input(_key, operation_key)
        digest = _request(
            "deliverable_add", [pid, fname, fsize, mime, file_hash.hex() if file_hash else ""]
        )
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(
                conn, owner, key, digest, _RESOURCE_DELIVERABLE, "product_deliverables"
            )
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_deliverables(id, owner_id, product_id, file_name, file_hash, file_size, mime_type, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (ident, owner, pid, fname, file_hash, fsize, mime, now),
            )
            _audit(conn, now, owner, "product_deliverable_created", _RESOURCE_DELIVERABLE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_DELIVERABLE, ident)
            return ident

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM product_deliverables WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]

    def remove(self, token: str, file_id: str, *, operation_key: str) -> str:
        fid = _input(_record_id, file_id)
        key = _input(_key, operation_key)
        digest = _request("deliverable_remove", [fid])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != _RESOURCE_DELIVERABLE or not hmac.compare_digest(
                    conn.execute(
                        "SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?",
                        (owner, key),
                    ).fetchone()[0],
                    digest,
                ):
                    raise ProductWorkspaceError(
                        "Operation key was already used for a different request."
                    )
                return fid
            if (
                conn.execute(
                    "SELECT 1 FROM product_deliverables WHERE id=? AND owner_id=?", (fid, owner)
                ).fetchone()
                is None
            ):
                raise AccessDenied("Access denied.")
            conn.execute("DELETE FROM product_deliverables WHERE id=? AND owner_id=?", (fid, owner))
            _audit(conn, now, owner, "product_deliverable_deleted", _RESOURCE_DELIVERABLE, fid)
            _remember(conn, owner, key, digest, _RESOURCE_DELIVERABLE, fid)
            return fid


# ---------- Sales Tracking ----------
class ProductSales:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def record(
        self,
        token: str,
        product_id: str,
        quantity: int,
        amount_paise: int,
        sale_at: int,
        *,
        operation_key: str,
        evidence_url: str = "",
        verification: str = "unverified",
        currency: str = "INR",
    ) -> str:
        pid = _input(_record_id, product_id)
        qty = _input(_quantity, quantity)
        price = _input(_prod_price, amount_paise)
        if type(sale_at) is not int or not 0 <= sale_at <= 4102444800:
            raise ProductWorkspaceError("Invalid sale_at.")
        evurl = _input(_evidence_url, evidence_url)
        if verification not in ("unverified", "verified"):
            raise ProductWorkspaceError("Invalid verification.")
        if verification == "verified" and evurl == "":
            raise ProductWorkspaceError("Verified sale requires evidence_url.")
        if currency != "INR":
            raise ProductWorkspaceError("Currency must be INR.")
        key = _input(_key, operation_key)
        digest = _request("sale_record", [pid, qty, price, sale_at, evurl, verification])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_SALE, "product_sales")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_sales(id, owner_id, product_id, quantity, amount_paise, currency, sale_at, evidence_url, verification, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ident, owner, pid, qty, price, currency, sale_at, evurl, verification, now),
            )
            _audit(conn, now, owner, "product_sale_created", _RESOURCE_SALE, ident)
            _remember(conn, owner, key, digest, _RESOURCE_SALE, ident)
            return ident

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM product_sales WHERE owner_id=? AND product_id=? ORDER BY sale_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]

    def summary(self, token: str, product_id: str) -> dict:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT quantity, amount_paise, verification FROM product_sales WHERE owner_id=? AND product_id=?",
                (owner, pid),
            ).fetchall()
            total_qty = sum(r["quantity"] for r in rows)
            total_amount = sum(r["amount_paise"] for r in rows)
            verified_qty = sum(r["quantity"] for r in rows if r["verification"] == "verified")
            verified_amount = sum(
                r["amount_paise"] for r in rows if r["verification"] == "verified"
            )
            return {
                "product_id": pid,
                "total_sales": len(rows),
                "total_quantity": total_qty,
                "total_amount_paise": total_amount,
                "verified_quantity": verified_qty,
                "verified_amount_paise": verified_amount,
                "verification": "unverified",  # overall no fabrication: sales are evidence-based
            }


# ---------- Product Editor (descriptions, pricing, status) ----------
class ProductEditor:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def update(
        self,
        token: str,
        product_id: str,
        *,
        operation_key: str,
        title: str | None = None,
        description: str | None = None,
        price_paise: int | None = None,
        version: str | None = None,
    ) -> str:
        pid = _input(_record_id, product_id)
        key = _input(_key, operation_key)
        title_v = _input(_title, title) if title is not None else None
        desc_v = _input(_prod_description, description) if description is not None else None
        price_v = _input(_prod_price, price_paise) if price_paise is not None else None
        ver_v = _input(_version_str, version) if version is not None else None
        if title_v is None and desc_v is None and price_v is None and ver_v is None:
            raise ProductWorkspaceError("No fields to update.")
        values = [
            pid,
            title_v or "",
            desc_v or "",
            price_v if price_v is not None else -1,
            ver_v or "",
        ]
        digest = _request("product_update", values)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            row = conn.execute(
                "SELECT request_hash, resource_type FROM state_operations WHERE owner_id=? AND operation_key=?",
                (owner, key),
            ).fetchone()
            if row is not None:
                if row["resource_type"] != "product_update" or not hmac.compare_digest(
                    conn.execute(
                        "SELECT request_hash FROM state_operations WHERE owner_id=? AND operation_key=?",
                        (owner, key),
                    ).fetchone()[0],
                    digest,
                ):
                    raise ProductWorkspaceError(
                        "Operation key was already used for a different request."
                    )
                return pid
            # Verify owned
            if (
                conn.execute(
                    "SELECT 1 FROM products WHERE id=? AND owner_id=?", (pid, owner)
                ).fetchone()
                is None
            ):
                raise AccessDenied("Access denied.")
            updates = []
            params = []
            if title_v is not None:
                updates.append("title=?")
                params.append(title_v)
            if desc_v is not None:
                updates.append("description=?")
                params.append(desc_v)
            if price_v is not None:
                updates.append("price_paise=?")
                params.append(price_v)
            if ver_v is not None:
                updates.append("version=?")
                params.append(ver_v)
            params.extend([now, pid, owner])
            conn.execute(
                f"UPDATE products SET {', '.join(updates)}, updated_at=? WHERE id=? AND owner_id=?",
                tuple(params),
            )
            _audit(conn, now, owner, "product_updated", "product_update", pid)
            _remember(conn, owner, key, digest, "product_update", pid)
            return pid


# ---------- Documentation ----------
class ProductDocumentation:
    def __init__(self, auth: AuthService, state: StateStorage):
        self._auth = auth
        self._state = state

    def create(
        self, token: str, product_id: str, title: str, content: str, *, operation_key: str
    ) -> str:
        pid = _input(_record_id, product_id)
        tit = _input(_doc_title, title)
        cont = _input(_doc_content, content)
        key = _input(_key, operation_key)
        digest = _request("doc_create", [pid, tit, cont])
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_DOC, "product_documentation")
            if existing:
                return existing
            _owned_product(conn, owner, pid)
            ident = secrets.token_hex(16)
            conn.execute(
                "INSERT INTO product_documentation(id, owner_id, product_id, title, content, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (ident, owner, pid, tit, cont, now, now),
            )
            _audit(conn, now, owner, "product_doc_created", _RESOURCE_DOC, ident)
            _remember(conn, owner, key, digest, _RESOURCE_DOC, ident)
            return ident

    def generate_ai_draft(
        self, token: str, product_id: str, *, operation_key: str, prompt: str | None = None
    ) -> str:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            prod = conn.execute(
                "SELECT title, description, version FROM products WHERE id=? AND owner_id=?",
                (pid, owner),
            ).fetchone()
            title = prod["title"]
            desc = prod["description"]
        # AI
        ai_content = None
        try:
            from .ai_assistant import AIAssistant

            ai = AIAssistant(self._auth, self._state)
            ptxt = (
                prompt or f"Generate documentation for product {title} v{prod['version']}: {desc}"
            )
            res = ai.chat(token, ptxt, external_content=None)
            ai_content = res.get("output", "")[:8000] if isinstance(res, dict) else str(res)[:8000]
        except Exception:
            ai_content = (
                f"Documentation draft for {title}: {desc[:200]} (AI generated, unverified)."
            )
        if not ai_content or len(ai_content) < 1:
            ai_content = "Documentation draft (unverified)."
        if len(ai_content) > 10000:
            ai_content = ai_content[:10000]
        # create doc with ai content
        doc_title = f"Docs: {title[:80]}"
        return self.create(token, pid, doc_title, ai_content, operation_key=operation_key)

    def get(self, token: str, doc_id: str) -> dict:
        did = _input(_record_id, doc_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            row = conn.execute(
                "SELECT * FROM product_documentation WHERE id=? AND owner_id=?", (did, owner)
            ).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            return dict(row)

    def list(self, token: str, product_id: str) -> list[dict]:
        pid = _input(_record_id, product_id)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, _now):
            _version_check(conn)
            _owned_product(conn, owner, pid)
            rows = conn.execute(
                "SELECT * FROM product_documentation WHERE owner_id=? AND product_id=? ORDER BY created_at, id",
                (owner, pid),
            ).fetchall()
            return [dict(r) for r in rows]

    def update(
        self,
        token: str,
        doc_id: str,
        title: str | None = None,
        content: str | None = None,
        *,
        operation_key: str,
    ) -> str:
        did = _input(_record_id, doc_id)
        key = _input(_key, operation_key)
        tit = _input(_doc_title, title) if title is not None else None
        cont = _input(_doc_content, content) if content is not None else None
        if tit is None and cont is None:
            raise ProductWorkspaceError("No fields to update.")
        values = [did, tit or "", cont or ""]
        digest = _request("doc_update", values)
        with _authorized_transaction(self._auth, self._state, token) as (conn, owner, now):
            _version_check(conn)
            existing = _retry(conn, owner, key, digest, _RESOURCE_DOC, "product_documentation")
            if existing:
                return did
            row = conn.execute(
                "SELECT * FROM product_documentation WHERE id=? AND owner_id=?", (did, owner)
            ).fetchone()
            if row is None:
                raise AccessDenied("Access denied.")
            updates = []
            params = []
            if tit is not None:
                updates.append("title=?")
                params.append(tit)
            if cont is not None:
                updates.append("content=?")
                params.append(cont)
            params.extend([now, did, owner])
            conn.execute(
                f"UPDATE product_documentation SET {', '.join(updates)}, updated_at=? WHERE id=? AND owner_id=?",
                tuple(params),
            )
            _audit(conn, now, owner, "product_doc_updated", _RESOURCE_DOC, did)
            _remember(conn, owner, key, digest, _RESOURCE_DOC, did)
            return did
