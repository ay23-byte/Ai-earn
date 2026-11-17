"""Extracted tracker menu handlers for choices 73-82."""
from .helpers import _read, _paise, _key

def handle_crm(choice, token, stdin, out, **deps):
    crm_notes = deps.get("crm_notes")
    crm_requirements = deps.get("crm_requirements")
    crm_orders = deps.get("crm_orders")
    crm_projects = deps.get("crm_projects")
    crm_comm_approvals = deps.get("crm_comm_approvals")
    crm_outbound = deps.get("crm_outbound")
    crm_comms = deps.get("crm_comms")
    crm_timeline = deps.get("crm_timeline")
    crm_profile = deps.get("crm_profile")
    crm_csv = deps.get("crm_csv")
    if choice == "73":
        cid=_read(stdin, out, "Customer ID: ").strip()
        try:
            prof=crm_profile.get(token, cid)
            out.write(f"Profile: {prof['name']} | {prof['contact']} | source:{prof['source']} | {prof['status']} | notes:{prof['_note_count']} req:{prof['_requirement_count']} comm:{prof['_communication_count']} follow:{prof['_followup_count']} orders:{prof['_order_count']} proj:{prof['_project_count']} prop:{prof['_proposal_count']}\n")
            full=crm_profile.full(token, cid)
            out.write(f"Timeline events: {len(full['timeline'])} | proposals:{len(full['proposals'])} | orders:{len(full['orders'])}\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "74":
        out.write("Notes: 1 Add  2 List  3 Get\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                cid=_read(stdin, out, "Customer ID: ").strip()
                note=_read(stdin, out, "Note (1..2000): ")
                nid=crm_notes.add(token, cid, note, operation_key=_key())
                out.write(f"Added note {nid}\n")
            elif sub=="2":
                cid=_read(stdin, out, "Customer ID: ").strip()
                lst=crm_notes.list(token, cid)
                for n in lst:
                    out.write(f"{n['id']} | {n['note'][:60]} | {n['created_at']}\n")
                if not lst:
                    out.write("No notes.\n")
            elif sub=="3":
                nid=_read(stdin, out, "Note ID: ").strip()
                n=crm_notes.get(token, nid)
                out.write(f"{n['id']} | cust:{n['customer_id'][:8]} | {n['note'][:120]}\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "75":
        out.write("Requirements: 1 Add  2 List  3 Remove\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                cid=_read(stdin, out, "Customer ID: ").strip()
                req=_read(stdin, out, "Requirement (1..500): ")
                rid=crm_requirements.add(token, cid, req, operation_key=_key())
                out.write(f"Added requirement {rid}\n")
            elif sub=="2":
                cid=_read(stdin, out, "Customer ID: ").strip()
                lst=crm_requirements.list(token, cid)
                for r in lst:
                    out.write(f"{r['id']} | {r['requirement'][:60]} | {r['created_at']}\n")
                if not lst:
                    out.write("No requirements.\n")
            elif sub=="3":
                rid=_read(stdin, out, "Requirement ID: ").strip()
                crm_requirements.remove(token, rid, operation_key=_key())
                out.write("Requirement removed.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "76":
        out.write("CRM Communications (approval-gated): 1 Create draft  2 Request approval  3 Approve/Reject  4 Send  5 List history\n")
        sub=_read(stdin, out, "Choice (1-5): ").strip()
        try:
            if sub=="1":
                cid=_read(stdin, out, "Customer ID: ").strip()
                chan=_read(stdin, out, "Channel (1..30): ")
                body=_read(stdin, out, "Body (1..2000): ")
                did=crm_comms.create_draft(token, cid, chan, body, operation_key=_key())
                out.write(f"Created draft {did} (requires approval unless safe workflow)\n")
            elif sub=="2":
                did=_read(stdin, out, "Communication ID: ").strip()
                ttl_raw=_read(stdin, out, "TTL 60..86400 (default 900): ").strip()
                ttl=int(ttl_raw) if ttl_raw else 900
                aid=crm_comm_approvals.request(token, did, operation_key=_key(), ttl=ttl)
                out.write(f"Approval requested {aid} (pending, expires in {ttl}s)\n")
            elif sub=="3":
                aid=_read(stdin, out, "Approval ID: ").strip()
                dec=_read(stdin, out, "Decision [approved/rejected]: ").strip()
                if dec=="approved":
                    crm_comm_approvals.approve(token, aid, operation_key=_key())
                    out.write("Approved.\n")
                elif dec=="rejected":
                    crm_comm_approvals.reject(token, aid, operation_key=_key())
                    out.write("Rejected.\n")
                else:
                    out.write("Invalid decision.\n")
            elif sub=="4":
                did=_read(stdin, out, "Communication ID: ").strip()
                aid=_read(stdin, out, "Approval ID (empty if safe workflow): ").strip() or None
                crm_comms.send(token, did, operation_key=_key(), approval_id=aid)
                out.write(f"Sent {did} (approval={aid} or safe workflow)\n")
            elif sub=="5":
                cid=_read(stdin, out, "Customer ID: ").strip()
                lst=crm_comms.list_history(token, cid, limit=20)
                for c in lst:
                    out.write(f"{c['id']} | {c['channel']} | {c['status']} | {c['body'][:40]}\n")
                if not lst:
                    out.write("No communications.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "77":
        out.write("Follow-ups: use 23 Add  24 List  25 Manage (CRM follow-ups are customer-scoped)\n")
        cid=_read(stdin, out, "Customer ID (empty for all): ").strip() or None
        try:
            from ai_earn.followups import Followups as _F
            fl=_F(crm_notes._auth, crm_notes._state)
            lst=fl.list(token, customer_id=cid, limit=20)
            for f in lst:
                out.write(f"{f['id']} | cust:{f['customer_id'][:8]} | due:{f['due_at']} | {f['status']} | {f['note'][:40]}\n")
            if not lst:
                out.write("No followups.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "78":
        out.write("Orders: 1 Create  2 List  3 Confirm  4 Fulfill  5 Cancel  6 Get\n")
        sub=_read(stdin, out, "Choice (1-6): ").strip()
        try:
            if sub=="1":
                cid=_read(stdin, out, "Customer ID: ").strip()
                title=_read(stdin, out, "Title (1..200): ")
                desc=_read(stdin, out, "Description (0..2000): ")
                amt=_paise(_read(stdin, out, "Amount INR: ").strip())
                oid=crm_orders.create(token, cid, title, desc, amt, operation_key=_key())
                out.write(f"Created order {oid} (draft)\n")
            elif sub=="2":
                cid=_read(stdin, out, "Customer ID (empty for all): ").strip() or None
                status=_read(stdin, out, "Filter status [draft/confirmed/fulfilled/cancelled or empty]: ").strip() or None
                lst=crm_orders.list(token, customer_id=cid, status=status, limit=20)
                for o in lst:
                    out.write(f"{o['id']} | cust:{o['customer_id'][:8]} | {o['title'][:30]} | INR {o['amount_paise']//100}.{o['amount_paise']%100:02d} | {o['status']}\n")
                if not lst:
                    out.write("No orders.\n")
            elif sub=="3":
                oid=_read(stdin, out, "Order ID: ").strip()
                crm_orders.confirm(token, oid, operation_key=_key())
                out.write("Confirmed.\n")
            elif sub=="4":
                oid=_read(stdin, out, "Order ID: ").strip()
                crm_orders.fulfill(token, oid, operation_key=_key())
                out.write("Fulfilled.\n")
            elif sub=="5":
                oid=_read(stdin, out, "Order ID: ").strip()
                crm_orders.cancel(token, oid, operation_key=_key())
                out.write("Cancelled.\n")
            elif sub=="6":
                oid=_read(stdin, out, "Order ID: ").strip()
                o=crm_orders.get(token, oid)
                out.write(f"{o['id']} | {o['title']} | {o['description'][:40]} | {o['status']}\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "79":
        out.write("Project history: 1 Link  2 List  3 Unlink\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                cid=_read(stdin, out, "Customer ID: ").strip()
                tid=_read(stdin, out, "Project task ID: ").strip()
                lid=crm_projects.link(token, cid, tid, operation_key=_key())
                out.write(f"Linked {lid}\n")
            elif sub=="2":
                cid=_read(stdin, out, "Customer ID: ").strip()
                lst=crm_projects.list(token, cid)
                for l in lst:
                    pt=l.get("project_task") or {}
                    out.write(f"{l['id']} | task:{l['project_task_id'][:8]} | {pt.get('title','')[:30]} | {pt.get('status','')}\n")
                if not lst:
                    out.write("No projects.\n")
            elif sub=="3":
                lid=_read(stdin, out, "Link ID: ").strip()
                crm_projects.unlink(token, lid, operation_key=_key())
                out.write("Unlinked.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "80":
        cid=_read(stdin, out, "Customer ID: ").strip()
        try:
            tl=crm_timeline.get(token, cid, limit=30)
            out.write(f"Timeline {len(tl)} events for {cid[:8]}:\n")
            for ev in tl:
                out.write(f"{ev['at']} | {ev['type']}/{ev['event']} | {ev['summary'][:60]}\n")
            if not tl:
                out.write("No timeline events.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "81":
        out.write("Outbound workflow (safe limited workflow): 1 Get  2 Set  3 Clear\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                cfg=crm_outbound.get(token)
                if cfg:
                    out.write(f"enabled:{cfg['enabled']} channels:{cfg['allowed_channels']} limit:{cfg['daily_limit']} updated:{cfg['updated_at']}\n")
                else:
                    out.write("No config (approval required for all outbound).\n")
            elif sub=="2":
                enabled_raw=_read(stdin, out, "Enabled [yes/no]: ").strip().lower()
                enabled=enabled_raw in ("yes","y","1","true")
                chans=_read(stdin, out, "Allowed channels comma-separated (0..500, empty for none): ")
                limit_raw=_read(stdin, out, "Daily limit 1..100 (default 5): ").strip()
                limit=int(limit_raw) if limit_raw else 5
                crm_outbound.set(token, operation_key=_key(), enabled=enabled, allowed_channels=chans, daily_limit=limit)
                out.write(f"Outbound config set enabled={enabled} channels={chans} limit={limit} (explicit safe workflow)\n")
            elif sub=="3":
                crm_outbound.clear(token, operation_key=_key())
                out.write("Outbound config cleared (approval required).\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "82":
        out.write("CSV (validated): 1 Export  2 Import\n")
        sub=_read(stdin, out, "Choice (1-2): ").strip()
        try:
            if sub=="1":
                csv_text=crm_csv.export(token)
                out.write("CSV export:\n")
                out.write(csv_text[:2000] + ("...\n" if len(csv_text)>2000 else "\n"))
            elif sub=="2":
                out.write("Paste CSV (header name,contact,source). End with line '.'\n")
                lines=[]
                while True:
                    line=_read(stdin, out, "")
                    if line==".":
                        break
                    lines.append(line)
                csv_text="\n".join(lines)
                prefix=_read(stdin, out, "Operation prefix 1..64: ").strip() or "csv_import"
                res=crm_csv.import_csv(token, csv_text, operation_key_prefix=prefix)
                out.write(f"Imported {len(res['created'])} customers\n")
                for iid in res['created']:
                    out.write(f" {iid}\n")
        except Exception as e:
            out.write(str(e) + "\n")
