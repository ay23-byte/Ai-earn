"""Authenticated workspace HTTP facade over existing owner-scoped domain services."""
from __future__ import annotations
import secrets
from typing import Any
from urllib.parse import parse_qs
from .customers import Customers
from .crm import CustomerNotes, CustomerProjects, CustomerRequirements
from .finance import ExpenseRecords, PaymentRecords
from .invoices import Invoices
from .ledger import Ledger
from .opportunities import Opportunities
from .order_drafts import OrderDrafts
from .products import Products
from .project_tasks import ProjectTasks
from .service_workspace import Proposals, ServiceEditor
from .services import Services

class WorkspaceAPIError(Exception):
    """A safe client-facing workspace API error."""

class WorkspaceAPI:
    def __init__(self, auth, state):
        self.opportunities = Opportunities(auth, state)
        self.services = Services(auth, state)
        self.service_editor = ServiceEditor(auth, state)
        self.proposals = Proposals(auth, state)
        self.customers = Customers(auth, state)
        self.customer_projects = CustomerProjects(auth, state)
        self.customer_notes = CustomerNotes(auth, state)
        self.customer_requirements = CustomerRequirements(auth, state)
        self.products = Products(auth, state)
        self.tasks = ProjectTasks(auth, state)
        self.orders = OrderDrafts(auth, state)
        self.invoices = Invoices(auth, state)
        self.payments = PaymentRecords(auth, state)
        self.expenses = ExpenseRecords(auth, state)
        self.ledger = Ledger(auth, state)

    @staticmethod
    def _key(payload: dict, operation_key: str | None) -> str:
        key = operation_key or payload.get("operation_key") or secrets.token_urlsafe(24)
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise WorkspaceAPIError("A valid idempotency key is required.")
        return key

    @staticmethod
    def _only(payload: dict, allowed: set[str]) -> dict:
        if set(payload) - allowed - {"operation_key"}:
            raise WorkspaceAPIError("Request contains unsupported fields.")
        return {k: v for k, v in payload.items() if k in allowed}

    @staticmethod
    def _page(query: dict[str, list[str]]) -> dict:
        result: dict[str, Any] = {}
        if "after_id" in query:
            result["after_id"] = query["after_id"][0]
        if "limit" in query:
            try:
                result["limit"] = int(query["limit"][0])
            except (TypeError, ValueError) as exc:
                raise WorkspaceAPIError("Invalid page size.") from exc
        if "status" in query:
            result["status"] = query["status"][0]
        return result

    def dispatch(self, method: str, path: str, token: str, payload: dict | None = None, query: str = "", operation_key: str | None = None) -> tuple[int, dict]:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 4 or parts[:3] != ["api", "v1", "workspace"]:
            return 404, self._error("not_found", "Workspace route not found.")
        resource = parts[3]
        payload = payload or {}
        if not isinstance(payload, dict):
            return 400, self._error("invalid_request", "A JSON object is required.")
        params = parse_qs(query, keep_blank_values=False)
        ident = parts[4] if len(parts) >= 5 else None
        try:
            key = self._key(payload, operation_key) if method in {"POST", "PATCH", "DELETE"} else ""

            if resource == "customers" and len(parts) == 6 and parts[5] in {"notes", "requirements"}:
                customer_id = parts[4]
                field = "note" if parts[5] == "notes" else "requirement"
                service = self.customer_notes if field == "note" else self.customer_requirements
                if method == "GET":
                    rows = service.list(token, customer_id)
                    return 200, self._ok({"items": rows, "count": len(rows)})
                if method == "POST":
                    values = self._only(payload, {field})
                    if set(values) != {field}:
                        raise WorkspaceAPIError(field + " is required.")
                    return 201, self._ok({"id": service.add(token, customer_id, values[field], operation_key=key)})
                return 405, self._error("method_not_allowed", "Client detail method is not supported.")
            if resource == "projects":
                if method == "GET" and len(parts) == 4:
                    page = self._page(params)
                    rows = self.customer_projects.list_all(token, **page)
                    return 200, self._ok({"items": rows, "count": len(rows),
                        "next_cursor": rows[-1]["id"] if len(rows) == page.get("limit", 20) else None})
                if method == "POST" and len(parts) == 4:
                    values = self._only(payload, {"customer_id", "title", "description", "priority", "deadline_at", "estimated_duration_seconds"})
                    if not isinstance(values.get("customer_id"), str) or not values["customer_id"]:
                        raise WorkspaceAPIError("customer_id is required.")
                    if not isinstance(values.get("title"), str) or not values["title"].strip():
                        raise WorkspaceAPIError("title is required.")
                    self.customers.get(token, values["customer_id"])
                    import hashlib
                    suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
                    task_id = self.tasks.create(token, values["title"],
                        description=values.get("description", ""), priority=values.get("priority", 5),
                        deadline_at=values.get("deadline_at", 0),
                        estimated_duration_seconds=values.get("estimated_duration_seconds", 0),
                        operation_key="p3-task-" + suffix)
                    link_id = self.customer_projects.link(token, values["customer_id"], task_id,
                        operation_key="p3-link-" + suffix)
                    return 201, self._ok({"id": task_id, "task_id": task_id, "link_id": link_id})
                if method == "POST" and len(parts) == 7 and parts[5] == "actions":
                    return 200, self._ok({"id": self._project_action(parts[6], token, ident or "", payload, key), "action": parts[6]})
                return 405, self._error("method_not_allowed", "Project route is not supported.")
            if method == "GET":
                if resource == "ledger" and len(parts) == 5 and parts[4] == "summary":
                    return 200, self._ok({"summary": self.ledger.net(token)})
                if len(parts) == 4:
                    page = self._page(params)
                    rows = self._list(resource, token, page)
                    return 200, self._ok({"items": rows, "count": len(rows), "next_cursor": rows[-1]["id"] if len(rows) == page.get("limit", 20) else None})
                if len(parts) == 5:
                    return 200, self._ok({"item": self._get(resource, token, ident or "")})
                return 404, self._error("not_found", "Workspace route not found.")
            if method == "POST" and len(parts) == 4:
                created = self._create(resource, token, self._only(payload, self._create_fields(resource)), key)
                return 201, self._ok({"id": created})
            if method == "PATCH" and len(parts) == 5 and resource == "services":
                fields = self._only(payload, {"title", "scope", "deliverable", "price_paise", "delivery_days", "revisions"})
                self.service_editor.update(token, ident or "", operation_key=key, **fields)
                return 200, self._ok({"id": ident, "updated": True})
            if method == "POST" and len(parts) == 7 and parts[5] == "actions":
                action = parts[6]
                result = self._action(resource, action, token, ident or "", payload, key)
                return 200, self._ok({"id": result, "action": action})
            return 405, self._error("method_not_allowed", "Method is not supported for this workspace route.")
        except WorkspaceAPIError as exc:
            return 400, self._error("invalid_request", str(exc))
        except Exception as exc:
            if type(exc).__name__ in {"OpportunityError", "ServiceError", "ServiceWorkspaceError", "CustomerError", "CRMError", "ProductError", "ProjectTaskError", "OrderError", "InvoiceError", "FinanceError", "LedgerError", "DraftError"}:
                return 400, self._error("domain_validation", str(exc))
            if type(exc).__name__ == "AccessDenied":
                return 404, self._error("not_found", "Record not found.")
            return 503, self._error("workspace_unavailable", "Workspace operation is temporarily unavailable.")


    def _project_action(self, action: str, token: str, task_id: str, payload: dict, key: str) -> str:
        actions = {"submit_for_review": self.tasks.submit_for_review, "approve": self.tasks.approve,
                   "reject": self.tasks.reject, "start": self.tasks.start,
                   "complete": self.tasks.complete, "cancel": self.tasks.cancel}
        fn = actions.get(action)
        if fn is None:
            raise WorkspaceAPIError("This project action is not supported.")
        if payload and set(payload) - {"operation_key"}:
            raise WorkspaceAPIError("This project action does not accept extra fields.")
        return fn(token, task_id, operation_key=key)

    @staticmethod
    def _ok(data: dict) -> dict:
        return {"ok": True, "data": data, "error": None}

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {"ok": False, "data": None, "error": {"code": code, "message": message}}

    @staticmethod
    def _create_fields(resource: str) -> set[str]:
        fields = {
            "opportunities": {"title", "source_url", "budget_paise", "deadline_at", "risk_level", "skill_tags", "source_type", "connector_id"},
            "services": {"title", "scope", "deliverable", "price_paise", "revisions"},
            "proposals": {"service_id", "title", "description", "price_paise", "delivery_days", "customer_id", "ai_generated"},
            "customers": {"name", "contact", "source"},
            "products": {"title", "description", "version", "price_paise"},
            "tasks": {"title", "description", "opportunity_id", "parent_task_id", "priority", "deadline_at", "estimated_duration_seconds"},
            "orders": {"title", "proposed_amount_paise", "proposal_id"},
            "invoices": {"order_id", "due_at", "billing_name", "billing_contact", "billing_address", "tax_rate_bps", "tax_amount_paise"},
            "payments": {"amount_paise", "provider", "provider_ref", "invoice_id", "order_id", "verified"},
            "expenses": {"amount_paise", "category", "note", "incurred_at", "verified"},
            "ledger": {"kind", "amount_paise", "note"},
        }
        if resource not in fields:
            raise WorkspaceAPIError("This workspace resource is not available through the local API yet.")
        return fields[resource]

    @staticmethod
    def _public_record(resource: str, record: dict) -> dict:
        """Convert domain-only binary fields into stable JSON-safe API values."""
        result = dict(record)
        if resource == "invoices" and isinstance(result.get("pdf_hash"), bytes):
            result["pdf_hash"] = result["pdf_hash"].hex()
        return result

    def _list(self, resource: str, token: str, page: dict) -> list[dict]:
        services = {"opportunities": self.opportunities, "services": self.services, "proposals": self.proposals, "customers": self.customers, "products": self.products, "tasks": self.tasks, "orders": self.orders, "invoices": self.invoices, "payments": self.payments, "expenses": self.expenses, "ledger": self.ledger}
        service = services.get(resource)
        if service is None:
            raise WorkspaceAPIError("Listing is not supported for this resource.")
        rows = service.list(token, **page)
        return [self._public_record(resource, row) for row in rows]

    def _get(self, resource: str, token: str, ident: str | None) -> dict:
        services = {"opportunities": self.opportunities, "services": self.services, "proposals": self.proposals, "customers": self.customers, "products": self.products, "tasks": self.tasks, "orders": self.orders, "invoices": self.invoices, "payments": self.payments, "expenses": self.expenses, "ledger": self.ledger}
        service = services.get(resource)
        if service is None or not ident:
            raise WorkspaceAPIError("Record lookup is not supported.")
        return self._public_record(resource, service.get(token, ident))

    def _create(self, resource: str, token: str, data: dict, key: str) -> str:
        if resource == "orders" and "proposal_id" in data:
            if set(data) != {"proposal_id"}:
                raise WorkspaceAPIError("proposal_id cannot be combined with manual order fields.")
            return self.orders.create_from_proposal(token, data["proposal_id"], operation_key=key)
        creators = {
            "opportunities": lambda: self.opportunities.create(token, operation_key=key, **data),
            "services": lambda: self.services.create(token, operation_key=key, **data),
            "proposals": lambda: self.proposals.create(token, operation_key=key, **data),
            "customers": lambda: self.customers.create(token, operation_key=key, **data),
            "products": lambda: self.products.create(token, operation_key=key, **data),
            "tasks": lambda: self.tasks.create(token, operation_key=key, **data),
            "orders": lambda: self.orders.create(token, operation_key=key, **data),
            "invoices": lambda: self.invoices.create(token, operation_key=key, **data),
            "payments": lambda: self.payments.create(token, operation_key=key, **data),
            "expenses": lambda: self.expenses.create(token, operation_key=key, **data),
            "ledger": lambda: self.ledger.add(token, operation_key=key, **data),
        }
        create = creators.get(resource)
        if create is None:
            raise WorkspaceAPIError("Create is not supported for this resource.")
        return create()

    def _action(self, resource: str, action: str, token: str, ident: str, payload: dict, key: str) -> str:
        actions = {
            "opportunities": {"vetted": self.opportunities.vetted, "pursuing": self.opportunities.pursuing},
            "services": {"publish": self.services.publish, "archive": self.services.archive},
            "proposals": {"send": self.proposals.send, "accept": self.proposals.accept, "reject": self.proposals.reject, "cancel": self.proposals.cancel, "archive": self.proposals.archive},
            "customers": {"contacted": self.customers.contacted, "converted": self.customers.converted, "rejected": self.customers.rejected},
            "products": {"publish": self.products.publish, "archive": self.products.archive},
            "tasks": {"approve": self.tasks.approve, "reject": self.tasks.reject, "start": self.tasks.start, "complete": self.tasks.complete, "cancel": self.tasks.cancel, "submit_for_review": self.tasks.submit_for_review},
            "orders": {"cancel": self.orders.cancel},
            "invoices": {"issue": self.invoices.issued},
            "payments": {"received": self.payments.mark_received, "settled": self.payments.mark_settled, "failed": self.payments.mark_failed, "refunded": self.payments.mark_refunded, "verify": self.payments.set_verified},
        }
        fn = actions.get(resource, {}).get(action)
        if fn is None:
            raise WorkspaceAPIError("This action is not supported for the resource.")
        if resource == "payments" and action in {"received", "settled"}:
            return fn(token, ident, operation_key=key, **self._only(payload, {"verified"}))
        if resource == "payments" and action == "verify":
            values = self._only(payload, {"verified"})
            if "verified" not in values:
                raise WorkspaceAPIError("verified is required.")
            return fn(token, ident, operation_key=key, **values)
        if payload and set(payload) - {"operation_key"}:
            raise WorkspaceAPIError("This action does not accept extra fields.")
        return fn(token, ident, operation_key=key)
