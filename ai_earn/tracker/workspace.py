"""Extracted tracker menu handlers for choices 49-58."""
import os
from pathlib import Path
from ai_earn.auth import AuthService
from ai_earn.auth_storage import AuthStorage
from ai_earn.state_storage import StateStorage
from .helpers import _read, _paise, _key

def handle_workspace(choice, token, stdin, out, **deps):
    services = deps.get("services")
    workspace_pkgs = deps.get("workspace_pkgs")
    workspace_reqs = deps.get("workspace_reqs")
    workspace_atts = deps.get("workspace_atts")
    proposals = deps.get("proposals")
    customer_requests = deps.get("customer_requests")
    service_editor = deps.get("service_editor")
    service_templates = deps.get("service_templates")
    portfolio_pres = deps.get("portfolio_pres")
    if choice == "49":
        sid = _read(stdin, out, "Service ID to edit: ").strip()
        try:
            cur = services.get(token, sid)
            out.write(f"Current: {cur['title']} | scope:{cur['scope'][:40]} | deliverable:{cur['deliverable'][:40]} | INR {cur['price_paise']//100}.{cur['price_paise']%100:02d} rev:{cur['revisions']}\n")
            title = _read(stdin, out, "New title (empty to keep): ").strip()
            if title == "":
                title = None
            scope = _read(stdin, out, "New scope (empty to keep): ").strip()
            if scope == "":
                scope = None
            deliverable = _read(stdin, out, "New deliverable (empty to keep): ").strip()
            if deliverable == "":
                deliverable = None
            price_raw = _read(stdin, out, "New price INR (empty to keep): ").strip()
            price = _paise(price_raw) if price_raw else None
            rev_raw = _read(stdin, out, "New revisions (empty to keep): ").strip()
            rev = int(rev_raw) if rev_raw else None
            dd_raw = _read(stdin, out, "Delivery days 1..365 (empty to keep): ").strip()
            dd = int(dd_raw) if dd_raw else None
            # Build kwargs
            kwargs={}
            if title is not None:
                kwargs["title"]=title
            if scope is not None:
                kwargs["scope"]=scope
            if deliverable is not None:
                kwargs["deliverable"]=deliverable
            if price is not None:
                kwargs["price_paise"]=price
            if rev is not None:
                kwargs["revisions"]=rev
            if dd is not None:
                kwargs["delivery_days"]=dd
            if not kwargs:
                out.write("No fields to update.\n")
            else:
                service_editor.update(token, sid, operation_key=_key(), **kwargs)
                out.write("Service updated.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "50":
        out.write("Packages: 1 Create  2 List  3 Update  4 Delete\n")
        sub=_read(stdin, out, "Choice (1-4): ").strip()
        try:
            if sub=="1":
                sid=_read(stdin, out, "Service ID: ").strip()
                title=_read(stdin, out, "Package title: ")
                desc=_read(stdin, out, "Description: ")
                price=_paise(_read(stdin, out, "Price INR: ").strip())
                dd=int(_read(stdin, out, "Delivery days 1..365: ").strip())
                rev=int(_read(stdin, out, "Revisions 0..100: ").strip())
                pid=workspace_pkgs.create(token, sid, title, desc, price, dd, rev, operation_key=_key())
                out.write(f"Created package {pid}\n")
            elif sub=="2":
                sid=_read(stdin, out, "Service ID: ").strip()
                pkgs=workspace_pkgs.list(token, sid)
                for p in pkgs:
                    out.write(f"{p['id']} | {p['title']} | {p['description'][:40]} | INR {p['price_paise']//100}.{p['price_paise']%100:02d} | {p['delivery_days']}d | rev:{p['revisions']}\n")
                if not pkgs:
                    out.write("No packages.\n")
            elif sub=="3":
                pid=_read(stdin, out, "Package ID: ").strip()
                title=_read(stdin, out, "New title (empty keep): ").strip() or None
                desc=_read(stdin, out, "New description (empty keep): ").strip() or None
                price_raw=_read(stdin, out, "New price (empty keep): ").strip()
                price=_paise(price_raw) if price_raw else None
                dd_raw=_read(stdin, out, "New delivery days (empty keep): ").strip()
                dd=int(dd_raw) if dd_raw else None
                rev_raw=_read(stdin, out, "New revisions (empty keep): ").strip()
                rev=int(rev_raw) if rev_raw else None
                kwargs={}
                if title: kwargs["title"]=title
                if desc: kwargs["description"]=desc
                if price is not None: kwargs["price_paise"]=price
                if dd is not None: kwargs["delivery_days"]=dd
                if rev is not None: kwargs["revisions"]=rev
                workspace_pkgs.update(token, pid, operation_key=_key(), **kwargs)
                out.write("Package updated.\n")
            elif sub=="4":
                pid=_read(stdin, out, "Package ID: ").strip()
                workspace_pkgs.delete(token, pid, operation_key=_key())
                out.write("Package deleted.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "51":
        out.write("Requirements: 1 Add  2 List  3 Remove\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                sid=_read(stdin, out, "Service ID: ").strip()
                req=_read(stdin, out, "Requirement (1..500): ")
                rid=workspace_reqs.add(token, sid, req, operation_key=_key())
                out.write(f"Added requirement {rid}\n")
            elif sub=="2":
                sid=_read(stdin, out, "Service ID: ").strip()
                reqs=workspace_reqs.list(token, sid)
                for r in reqs:
                    out.write(f"{r['id']} | {r['requirement'][:60]}\n")
                if not reqs:
                    out.write("No requirements.\n")
            elif sub=="3":
                rid=_read(stdin, out, "Requirement ID: ").strip()
                workspace_reqs.remove(token, rid, operation_key=_key())
                out.write("Requirement removed.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "52":
        out.write("Attachments: 1 Add  2 List  3 Remove\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                sid=_read(stdin, out, "Service ID (empty for none): ").strip() or None
                pid=_read(stdin, out, "Portfolio item ID (empty for none): ").strip() or None
                fname=_read(stdin, out, "File name (1..200): ")
                fsize=int(_read(stdin, out, "File size 0..104857600: ").strip() or "0")
                mime=_read(stdin, out, "MIME type (e.g. application/pdf): ").strip() or "application/octet-stream"
                aid=workspace_atts.add(token, operation_key=_key(), file_name=fname, file_size=fsize, mime_type=mime, service_id=sid, portfolio_item_id=pid)
                out.write(f"Added attachment {aid}\n")
            elif sub=="2":
                sid=_read(stdin, out, "Service ID (empty for none): ").strip() or None
                pid=_read(stdin, out, "Portfolio item ID (empty for none): ").strip() or None
                atts=workspace_atts.list(token, service_id=sid, portfolio_item_id=pid)
                for a in atts:
                    out.write(f"{a['id']} | {a['file_name']} | {a['file_size']}b | {a['mime_type']} | svc:{(a['service_id'] or '-')[:8]} port:{(a['portfolio_item_id'] or '-')[:8]}\n")
                if not atts:
                    out.write("No attachments.\n")
            elif sub=="3":
                aid=_read(stdin, out, "Attachment ID: ").strip()
                workspace_atts.remove(token, aid, operation_key=_key())
                out.write("Attachment removed.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "53":
        sid=_read(stdin, out, "Service ID: ").strip()
        cid=_read(stdin, out, "Customer ID (empty for none): ").strip() or None
        title=_read(stdin, out, "Proposal title: ")
        desc=_read(stdin, out, "Description: ")
        price=_paise(_read(stdin, out, "Price INR: ").strip())
        dd=int(_read(stdin, out, "Delivery days 1..365: ").strip())
        try:
            pid=proposals.create(token, sid, title, desc, price, dd, operation_key=_key(), customer_id=cid)
            out.write(f"Created proposal {pid} (draft)\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "54":
        sid=_read(stdin, out, "Service ID: ").strip()
        cid=_read(stdin, out, "Customer ID (empty for none): ").strip() or None
        prompt=_read(stdin, out, "Prompt for AI (empty for default): ").strip() or None
        try:
            pid=proposals.generate_ai_draft(token, sid, operation_key=_key(), customer_id=cid, prompt=prompt)
            out.write(f"AI draft proposal {pid} (unverified draft)\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "55":
        out.write("Proposals: 1 List  2 Manage status\n")
        sub=_read(stdin, out, "Choice (1-2): ").strip()
        try:
            if sub=="1":
                status=_read(stdin, out, "Filter status [draft/sent/accepted/rejected/cancelled/archived or empty]: ").strip() or None
                proposals_list=proposals.list(token, status=status)
                for p in proposals_list:
                    out.write(f"{p['id']} | svc:{p['service_id'][:8]} cust:{(p['customer_id'] or '-')[:8]} | {p['title'][:40]} | INR {p['price_paise']//100}.{p['price_paise']%100:02d} | {p['delivery_days']}d | {p['status']} ai:{p['ai_generated']}\n")
                if not proposals_list:
                    out.write("No proposals.\n")
            elif sub=="2":
                pid=_read(stdin, out, "Proposal ID: ").strip()
                cur=proposals.get(token, pid)
                out.write(f"Current: {cur['title']} | {cur['status']} | ai:{cur['ai_generated']}\n")
                out.write("Transitions: 1 send  2 accept  3 reject  4 cancel  5 archive\n")
                tc=_read(stdin, out, "Choice (1-5): ").strip()
                if tc=="1":
                    proposals.send(token, pid, operation_key=_key())
                    out.write("Sent.\n")
                elif tc=="2":
                    proposals.accept(token, pid, operation_key=_key())
                    out.write("Accepted.\n")
                elif tc=="3":
                    proposals.reject(token, pid, operation_key=_key())
                    out.write("Rejected.\n")
                elif tc=="4":
                    proposals.cancel(token, pid, operation_key=_key())
                    out.write("Cancelled.\n")
                elif tc=="5":
                    proposals.archive(token, pid, operation_key=_key())
                    out.write("Archived.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "56":
        out.write("Customer requests: 1 Create  2 List  3 Manage\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                sid=_read(stdin, out, "Service ID: ").strip()
                cid=_read(stdin, out, "Customer ID: ").strip()
                msg=_read(stdin, out, "Message (1..2000): ")
                rid=customer_requests.create(token, sid, cid, msg, operation_key=_key())
                out.write(f"Created request {rid} (pending)\n")
            elif sub=="2":
                status=_read(stdin, out, "Filter status [pending/accepted/rejected/completed/cancelled or empty]: ").strip() or None
                lst=customer_requests.list(token, status=status)
                for r in lst:
                    out.write(f"{r['id']} | svc:{r['service_id'][:8]} cust:{r['customer_id'][:8]} | {r['status']} | {r['message'][:40]}\n")
                if not lst:
                    out.write("No requests.\n")
            elif sub=="3":
                rid=_read(stdin, out, "Request ID: ").strip()
                cur=customer_requests.get(token, rid)
                out.write(f"Current: {cur['status']} | {cur['message'][:40]}\n")
                out.write("Transitions: 1 accept  2 reject  3 complete  4 cancel\n")
                tc=_read(stdin, out, "Choice (1-4): ").strip()
                if tc=="1":
                    customer_requests.accept(token, rid, operation_key=_key())
                    out.write("Accepted.\n")
                elif tc=="2":
                    customer_requests.reject(token, rid, operation_key=_key())
                    out.write("Rejected.\n")
                elif tc=="3":
                    customer_requests.complete(token, rid, operation_key=_key())
                    out.write("Completed.\n")
                elif tc=="4":
                    customer_requests.cancel(token, rid, operation_key=_key())
                    out.write("Cancelled.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "57":
        out.write("Templates: Excel data cleaning, Data entry, CSV processing, Python automation, Data analysis, Website development, Frontend development, AI workflow automation, Document and invoice automation\n")
        out.write("1 List templates  2 View template  3 Create service from template\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                for tid in service_templates.list_templates():
                    t=service_templates.get_template(tid)
                    out.write(f"{tid} | {t['title']} | INR {t['price_paise']//100}.{t['price_paise']%100:02d} | {t['delivery_days']}d rev:{t['revisions']}\n")
            elif sub=="2":
                tid=_read(stdin, out, "Template ID: ").strip()
                t=service_templates.get_template(tid)
                out.write(f"Title: {t['title']}\nScope: {t['scope'][:200]}\nDeliverable: {t['deliverable'][:200]}\nPackages: {len(t['packages'])} Requirements: {len(t['requirements'])}\n")
            elif sub=="3":
                tid=_read(stdin, out, "Template ID: ").strip()
                title=_read(stdin, out, "Custom title (empty to keep template title): ").strip() or None
                sid=service_templates.create_service_from_template(AuthService(AuthStorage(Path(os.environ.get('AI_EARN_AUTH_DB'))), create=False), StateStorage(Path(os.environ.get('AI_EARN_STATE_DB'))), token, tid, operation_key=_key(), title=title)
                out.write(f"Created service {sid} from template {tid} (editable)\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "58":
        sid=_read(stdin, out, "Service ID (empty for all): ").strip() or None
        try:
            pres=portfolio_pres.render(token, sid)
            out.write(f"Portfolio presentation: {pres['total_services']} service(s), verification:{pres['verification']}\n")
            for svc in pres["services"]:
                s=svc["service"]
                out.write(f"\n=== {s['title']} ===\n")
                out.write(f"Scope: {s['scope'][:120]}\nDeliverable: {s['deliverable'][:120]}\n{svc['presentation']['price_display']} | {svc['presentation']['packages_display'][:120]}\n")
                out.write(f"Packages: {len(svc['packages'])} Requirements: {len(svc['requirements'])} Attachments: {len(svc['attachments'])} Portfolio items: {len(svc['portfolio_items'])}\n")
                for item in svc["portfolio_items"]:
                    out.write(f" - Portfolio: {item['title']} | {item['description'][:60]} | attachments:{len(item['attachments'])}\n")
        except Exception as e:
            out.write(str(e) + "\n")

