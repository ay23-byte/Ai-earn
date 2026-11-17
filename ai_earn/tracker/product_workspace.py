"""Extracted tracker menu handlers for choices 62-72."""
from .helpers import _read, _paise, _key

def handle_product_workspace(choice, token, stdin, out, **deps):
    products = deps.get("products")
    product_ideas = deps.get("product_ideas")
    product_validations = deps.get("product_validations")
    market_research = deps.get("market_research")
    pricing_suggestions = deps.get("pricing_suggestions")
    product_versions = deps.get("product_versions")
    product_files = deps.get("product_files")
    product_deliverables = deps.get("product_deliverables")
    product_sales = deps.get("product_sales")
    product_docs = deps.get("product_docs")
    product_editor = deps.get("product_editor")
    if choice == "62":
        out.write("Ideas: 1 Create  2 List  3 Validate/Reject/Archive\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                title=_read(stdin, out, "Idea title (1..200): ")
                desc=_read(stdin, out, "Description (0..2000, empty for none): ")
                iid=product_ideas.create(token, title, desc, operation_key=_key())
                out.write(f"Created idea {iid} (idea)\n")
            elif sub=="2":
                status=_read(stdin, out, "Filter status [idea/validated/rejected/archived or empty]: ").strip() or None
                lst=product_ideas.list(token, status=status)
                for it in lst:
                    out.write(f"{it['id']} | {it['title'][:40]} | {it['status']} | {it['description'][:40]}\n")
                if not lst:
                    out.write("No ideas.\n")
            elif sub=="3":
                iid=_read(stdin, out, "Idea ID: ").strip()
                cur=product_ideas.get(token, iid)
                out.write(f"Current: {cur['title']} | {cur['status']}\nTransitions: 1 validated 2 rejected 3 archived\n")
                tc=_read(stdin, out, "Choice (1-3): ").strip()
                if tc=="1":
                    product_ideas.validate(token, iid, operation_key=_key())
                    out.write("Validated.\n")
                elif tc=="2":
                    product_ideas.reject(token, iid, operation_key=_key())
                    out.write("Rejected.\n")
                elif tc=="3":
                    product_ideas.archive(token, iid, operation_key=_key())
                    out.write("Archived.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "63":
        prod_id=_read(stdin, out, "Product ID: ").strip()
        out.write("Validations: 1 Add  2 List\n")
        sub=_read(stdin, out, "Choice (1-2): ").strip()
        try:
            if sub=="1":
                crit=_read(stdin, out, "Criteria (1..500): ")
                res=_read(stdin, out, "Result [pass/fail/pending]: ").strip() or "pending"
                ev=_read(stdin, out, "Evidence (0..1000, empty for none): ")
                vid=product_validations.add(token, prod_id, crit, res, operation_key=_key(), evidence=ev)
                out.write(f"Added validation {vid} ({res})\n")
            elif sub=="2":
                lst=product_validations.list(token, prod_id)
                for r in lst:
                    out.write(f"{r['id']} | {r['criteria'][:40]} | {r['result']} | {r['evidence'][:30]}\n")
                if not lst:
                    out.write("No validations.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "64":
        prod_id=_read(stdin, out, "Product ID: ").strip()
        out.write("Market research: 1 Add  2 AI draft  3 List\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                q=_read(stdin, out, "Query (1..500): ")
                f=_read(stdin, out, "Findings (1..5000): ")
                ev=_read(stdin, out, "Evidence URL (https://, empty for none): ").strip()
                ver=_read(stdin, out, "Verification [unverified/verified, default unverified]: ").strip() or "unverified"
                rid=market_research.add(token, prod_id, q, f, operation_key=_key(), evidence_url=ev, verification=ver)
                out.write(f"Added research {rid} ({ver})\n")
                if ver=="unverified":
                    out.write("Note: unverified drafts must not be presented as fact.\n")
            elif sub=="2":
                q=_read(stdin, out, "AI query (1..500): ")
                ev=_read(stdin, out, "Evidence URL (empty for none): ").strip()
                rid=market_research.generate_ai_draft(token, prod_id, q, operation_key=_key(), evidence_url=ev)
                out.write(f"AI draft research {rid} (ai_generated=1, unverified)\n")
            elif sub=="3":
                lst=market_research.list(token, prod_id)
                for r in lst:
                    out.write(f"{r['id']} | q:{r['query'][:30]} | ai:{r['ai_generated']} {r['verification']} | {r['findings'][:60]}\n")
                if not lst:
                    out.write("No research.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "65":
        prod_id=_read(stdin, out, "Product ID: ").strip()
        out.write("Pricing: 1 Add  2 AI suggestion  3 List\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                price=_paise(_read(stdin, out, "Price INR: ").strip())
                rat=_read(stdin, out, "Rationale (1..2000): ")
                ev=_read(stdin, out, "Evidence (0..1000, empty for none): ")
                pid=pricing_suggestions.add(token, prod_id, price, rat, operation_key=_key(), evidence=ev, ai_generated=False)
                out.write(f"Added pricing {pid}\n")
            elif sub=="2":
                pid=pricing_suggestions.generate_ai_suggestion(token, prod_id, operation_key=_key())
                out.write(f"AI pricing suggestion {pid} (unverified draft)\n")
            elif sub=="3":
                lst=pricing_suggestions.list(token, prod_id)
                for r in lst:
                    out.write(f"{r['id']} | INR {r['price_paise']//100}.{r['price_paise']%100:02d} | ai:{r['ai_generated']} | {r['rationale'][:40]}\n")
                if not lst:
                    out.write("No pricing.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "66":
        prod_id=_read(stdin, out, "Product ID: ").strip()
        out.write("Versions: 1 Add  2 List\n")
        sub=_read(stdin, out, "Choice (1-2): ").strip()
        try:
            if sub=="1":
                ver=_read(stdin, out, "Version [A-Za-z0-9._- 1..20]: ").strip()
                notes=_read(stdin, out, "Notes (0..2000): ")
                vid=product_versions.add(token, prod_id, ver, notes, operation_key=_key())
                out.write(f"Added version {vid} -> product now v{ver}\n")
            elif sub=="2":
                lst=product_versions.list(token, prod_id)
                for r in lst:
                    out.write(f"{r['id']} | v{r['version']} | {r['notes'][:40]}\n")
                if not lst:
                    out.write("No versions.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "67":
        out.write("Files: 1 Add  2 List  3 Remove\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                pid=_read(stdin, out, "Product ID: ").strip()
                fname=_read(stdin, out, "File name (1..200): ")
                fsize=int(_read(stdin, out, "File size 0..104857600: ").strip() or "0")
                mime=_read(stdin, out, "MIME (e.g. application/pdf): ").strip() or "application/octet-stream"
                fid=product_files.add(token, pid, fname, fsize, mime, operation_key=_key())
                out.write(f"Added file {fid}\n")
            elif sub=="2":
                pid=_read(stdin, out, "Product ID: ").strip()
                lst=product_files.list(token, pid)
                for r in lst:
                    out.write(f"{r['id']} | {r['file_name']} | {r['file_size']}b | {r['mime_type']}\n")
                if not lst:
                    out.write("No files.\n")
            elif sub=="3":
                fid=_read(stdin, out, "File ID: ").strip()
                product_files.remove(token, fid, operation_key=_key())
                out.write("File removed.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "68":
        prod_id=_read(stdin, out, "Product ID: ").strip()
        out.write("Sales: 1 Record  2 List  3 Summary\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                qty=int(_read(stdin, out, "Quantity 1..1000000: ").strip())
                amt=_paise(_read(stdin, out, "Amount INR total: ").strip())
                sale_at=int(_read(stdin, out, "Sale unix ts: ").strip())
                ev=_read(stdin, out, "Evidence URL (https://, required if verified): ").strip()
                ver=_read(stdin, out, "Verification [unverified/verified]: ").strip() or "unverified"
                sid=product_sales.record(token, prod_id, qty, amt, sale_at, operation_key=_key(), evidence_url=ev, verification=ver)
                out.write(f"Recorded sale {sid} ({ver}) qty {qty}\n")
                if ver=="unverified":
                    out.write("Note: unverified, not claimed as received.\n")
            elif sub=="2":
                lst=product_sales.list(token, prod_id)
                for r in lst:
                    out.write(f"{r['id']} | qty:{r['quantity']} INR {r['amount_paise']//100}.{r['amount_paise']%100:02d} | {r['verification']} | {r['sale_at']}\n")
                if not lst:
                    out.write("No sales.\n")
            elif sub=="3":
                summ=product_sales.summary(token, prod_id)
                out.write(f"Sales summary: total qty {summ['total_quantity']} verified {summ['verified_quantity']} sales {summ['total_sales']} verified {summ['verified_sales']} verification:{summ['verification']}\n")
                out.write("Policy: verified requires evidence_url; unverified not claimed as received.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "69":
        out.write("Deliverables: 1 Add  2 List  3 Remove\n")
        sub=_read(stdin, out, "Choice (1-3): ").strip()
        try:
            if sub=="1":
                pid=_read(stdin, out, "Product ID: ").strip()
                fname=_read(stdin, out, "File name (1..200): ")
                fsize=int(_read(stdin, out, "File size: ").strip() or "0")
                mime=_read(stdin, out, "MIME: ").strip() or "application/octet-stream"
                did=product_deliverables.add(token, pid, fname, fsize, mime, operation_key=_key())
                out.write(f"Added deliverable {did}\n")
            elif sub=="2":
                pid=_read(stdin, out, "Product ID: ").strip()
                lst=product_deliverables.list(token, pid)
                for r in lst:
                    out.write(f"{r['id']} | {r['file_name']} | {r['file_size']}b | {r['mime_type']}\n")
                if not lst:
                    out.write("No deliverables.\n")
            elif sub=="3":
                did=_read(stdin, out, "Deliverable ID: ").strip()
                product_deliverables.remove(token, did, operation_key=_key())
                out.write("Deliverable removed.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "70":
        out.write("Documentation: 1 Create  2 AI draft  3 List  4 Update  5 Get\n")
        sub=_read(stdin, out, "Choice (1-5): ").strip()
        try:
            if sub=="1":
                pid=_read(stdin, out, "Product ID: ").strip()
                title=_read(stdin, out, "Title (1..200): ")
                content=_read(stdin, out, "Content (1..10000): ")
                did=product_docs.create(token, pid, title, content, operation_key=_key())
                out.write(f"Created doc {did}\n")
            elif sub=="2":
                pid=_read(stdin, out, "Product ID: ").strip()
                did=product_docs.generate_ai_draft(token, pid, operation_key=_key())
                out.write(f"AI draft doc {did} (ai_generated=1, unverified)\n")
            elif sub=="3":
                pid=_read(stdin, out, "Product ID: ").strip()
                lst=product_docs.list(token, pid)
                for r in lst:
                    out.write(f"{r['id']} | {r['title'][:40]} | ai:{r['ai_generated']} {r['verification']}\n")
                if not lst:
                    out.write("No docs.\n")
            elif sub=="4":
                did=_read(stdin, out, "Doc ID: ").strip()
                title=_read(stdin, out, "New title (empty keep): ").strip() or None
                content=_read(stdin, out, "New content (empty keep): ").strip() or None
                kwargs={}
                if title: kwargs["title"]=title
                if content: kwargs["content"]=content
                if kwargs:
                    product_docs.update(token, did, operation_key=_key(), **kwargs)
                    out.write("Doc updated.\n")
                else:
                    out.write("No fields.\n")
            elif sub=="5":
                did=_read(stdin, out, "Doc ID: ").strip()
                doc=product_docs.get(token, did)
                out.write(f"{doc['title']}: {doc['content'][:200]} verification:{doc['verification']}\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "71":
        pid=_read(stdin, out, "Product ID: ").strip()
        try:
            cur=products.get(token, pid)
            out.write(f"Current: {cur['title']} | {cur['description'][:40]} | INR {cur['price_paise']//100}.{cur['price_paise']%100:02d} | v{cur['version']}\n")
            title=_read(stdin, out, "New title (empty keep): ").strip() or None
            desc=_read(stdin, out, "New description (empty keep): ").strip() or None
            price_raw=_read(stdin, out, "New price INR (empty keep): ").strip()
            price=_paise(price_raw) if price_raw else None
            ver=_read(stdin, out, "New version (empty keep): ").strip() or None
            kwargs={}
            if title: kwargs["title"]=title
            if desc: kwargs["description"]=desc
            if price is not None: kwargs["price_paise"]=price
            if ver: kwargs["version"]=ver
            if kwargs:
                product_editor.update(token, pid, operation_key=_key(), **kwargs)
                out.write("Product updated.\n")
            else:
                out.write("No fields.\n")
        except Exception as e:
            out.write(str(e) + "\n")
    elif choice == "72":
        pid=_read(stdin, out, "Product ID: ").strip()
        try:
            cur=products.get(token, pid)
            out.write(f"Current: {cur['status']} | {cur['title']}\nTransitions: 1 publish 2 archive\n")
            tc=_read(stdin, out, "Choice (1-2): ").strip()
            if tc=="1":
                products.publish(token, pid, operation_key=_key())
                out.write("Published.\n")
            elif tc=="2":
                products.archive(token, pid, operation_key=_key())
                out.write("Archived.\n")
        except Exception as e:
            out.write(str(e) + "\n")
