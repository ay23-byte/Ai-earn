// Earn OS SPA — vanilla hash router, theme toggle, snapshot renderer. No network requests.
"use strict";
const $ = (id) => document.getElementById(id);
const count = (v) => Number.isSafeInteger(v) && v >= 0 ? String(v) : "0";
const rupees = (v) => {
  const n = Number.isSafeInteger(v) && v >= 0 ? v : 0;
  try { return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(n / 100); } catch (_) { return "INR " + (n/100).toFixed(2); }
};
const ROUTES = ["overview","opportunities","services","portfolio","products","customers","communications","followups","ledger","invoices","provider","analytics","ai-approvals","connectors","queue","workflows","diagnostics"];
let currentSnapshot = null;
let currentMetrics = null;

// Theme: system + toggle persisted in localStorage (no cookie/auth)
function applyTheme(theme) {
  const t = theme === "light" || theme === "dark" ? theme : (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  document.documentElement.setAttribute("data-theme", t);
  const btn = $("themeToggle");
  if (btn) {
    const isLight = t === "light";
    btn.textContent = isLight ? "Dark mode" : "Light mode";
    btn.setAttribute("aria-pressed", String(isLight));
  }
}
function initTheme() {
  const saved = (()=>{ try{ return localStorage.getItem("earn_os_theme"); }catch(_){ return null; }})();
  applyTheme(saved);
  const btn = $("themeToggle");
  if (btn) btn.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme") || "dark";
    const next = cur === "dark" ? "light" : "dark";
    try{ localStorage.setItem("earn_os_theme", next); }catch(_){}
    applyTheme(next);
  });
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
      try{
        if (!localStorage.getItem("earn_os_theme")) applyTheme(null);
      }catch(_){}
    });
  }
}

// Nav toggle for mobile
function initNavToggle(){
  const btn = $("navToggle"), nav = $("primaryNav");
  if (!btn || !nav) return;
  btn.addEventListener("click", ()=>{
    const open = nav.classList.toggle("open");
    btn.setAttribute("aria-expanded", String(open));
  });
}

// Router
function normalizeRoute(hash){
  const raw = (hash || "").replace(/^#\/?/, "").split("?")[0].split("/")[0].trim().toLowerCase();
  if (ROUTES.includes(raw)) return raw;
  if (!raw) return "overview";
  return "overview";
}
function updateRoute(){
  const route = normalizeRoute(location.hash);
  document.querySelectorAll("[data-route]").forEach(a=>{
    const is = a.getAttribute("data-route") === route;
    if (is) a.setAttribute("aria-current","page"); else a.removeAttribute("aria-current");
  });
  document.querySelectorAll(".view").forEach(v=>{
    const vd = v.getAttribute("data-view");
    const active = vd === route;
    v.classList.toggle("active", active);
    if (active) v.removeAttribute("hidden"); else v.setAttribute("hidden","");
  });
  const main = $("main");
  if (main) main.focus({ preventScroll: true });
}
function initRouter(){
  window.addEventListener("hashchange", updateRoute);
  updateRoute();
}

// Rendering helpers
function renderByStatus(containerId, obj){
  const el = $(containerId);
  if (!el) return;
  if (!obj || typeof obj !== "object" || Object.keys(obj).length === 0) {
    el.innerHTML = '<div class="empty">Empty — no records in this snapshot.</div>';
    return;
  }
  const rows = Object.entries(obj).map(([k,v])=>{
    const cnt = v && typeof v.count === "number" ? count(v.count) : "0";
    const amt = v && typeof v.amount_paise === "number" ? " · " + rupees(v.amount_paise) : "";
    return '<div class="row"><div><b>'+escapeHtml(k)+'</b><small>'+cnt+' record(s)'+escapeHtml(amt)+'</small></div><strong>'+cnt+'</strong></div>';
  }).join("");
  el.innerHTML = rows || '<div class="empty">Empty — no records.</div>';
}
function escapeHtml(s){ return String(s).replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function setText(id, text){ const el=$(id); if(el) el.textContent=text; }

function render(snapshot){
  if (!snapshot || snapshot.format !== "ai-earn-dashboard-snapshot" || snapshot.version !== 1 || !snapshot.metrics || snapshot.metrics.read_only !== true) throw Error("Unsupported snapshot. Expected format ai-earn-dashboard-snapshot version 1 with read_only metrics.");
  const m = snapshot.metrics;
  const w = m.work||{}, c=m.commercial||{}, p=m.asserted_pipeline||{}, o=m.payment_observations||{}, inc=m.income||{}, opp=m.opportunities||{}, svc=m.services||{}, pf=m.portfolio||{}, prod=m.products||{}, cust=m.customers||{}, comm=m.communications||{}, led=m.ledger||{}, foll=m.followups||{}, inv=m.invoices||{}, leg11=m.ledger_11||{}, prov=m.provider_orders||{}, provevt=m.provider_events||{}, aia=m.ai_approvals||{}, eme=m.ai_emergency||{}, ana=m.analytics||{};
  currentMetrics = m;
  // Overview
  setText("metricPipeline", rupees(p.amount_paise));
  setText("metricPipelineNote", count(p.count)+" unverified assertion(s), not revenue");
  setText("metricInvoices", rupees((c.invoice_drafts||{}).amount_paise));
  setText("metricInvoicesNote", count((c.invoice_drafts||{}).count)+" invoice draft(s), not issued");
  setText("metricObservations", rupees(o.amount_paise));
  setText("metricObservationsNote", count(o.count)+" unverified observation(s)");
  setText("metricIncome", inc.status==="not_available_no_verified_payment_ledger" ? "Not available" : rupees(inc.verified_amount_paise));
  setText("metricIncomeNote", inc.note||"No verified payment ledger");
  setText("workOpps", count(opp.count));
  setText("workServices", count(svc.count));
  setText("workProducts", count(prod.count));
  setText("workCustomers", count(cust.count));
  setText("workLedgerNet", rupees(led.net_paise !== undefined ? led.net_paise : 0));
  setText("workInvoicesIssued", count(inv.count));
  setText("workProvider", count(prov.count));
  setText("workAI", count(aia.count));
  setText("workFunnel", ana.funnel ? (count(ana.funnel.opportunities)+"/"+count(ana.funnel.customers)+"/"+count(ana.funnel.invoices)) : "—");
  setText("pipelineStatus", count(p.count)+" asserted record(s), not verified");
  setText("ledger11Status", leg11.proposed ? count(leg11.proposed.count)+" proposed, "+count((leg11.invoices_issued||{}).count)+" issued, net "+rupees((leg11.ledger_net_unverified||{}).amount_paise||0) : "No 11-concept");
  setText("providerStatus", count(prov.count)+" provider order(s), sandbox");
  setText("incomeStatus", inc.status==="not_available_no_verified_payment_ledger" ? "No verified payment ledger" : "Verification: "+(inc.status||"unknown"));
  // Opportunities view
  setText("oppCount", count(opp.count));
  setText("oppAmount", rupees(opp.amount_paise||0));
  setText("oppStatus", count(opp.count)+" discovered (unverified), budget "+rupees(opp.amount_paise||0));
  renderByStatus("oppByStatus", opp.statuses);
  // Services
  setText("svcCount", count(svc.count));
  setText("svcAmount", rupees(svc.amount_paise||0));
  setText("svcStatus", count(svc.count)+" service draft(s), unverified");
  renderByStatus("svcByStatus", svc.statuses);
  // Portfolio
  setText("portfolioCount", count(pf.count));
  setText("portfolioStatus", count(pf.count)+" demo item(s)");
  // Products
  setText("prodCount", count(prod.count));
  setText("prodAmount", rupees(prod.amount_paise||0));
  setText("prodStatus", count(prod.count)+" product draft(s), unverified");
  renderByStatus("prodByStatus", prod.statuses);
  // Customers
  setText("custCount", count(cust.count));
  const custStatuses = cust.statuses || {};
  setText("custStatus", count(cust.count)+" lead(s), unverified · "+Object.entries(custStatuses).map(([k,v])=>k+":"+count(v.count)).join(" "));
  renderByStatus("custByStatus", custStatuses);
  // Communications
  setText("commCount", count(comm.count));
  setText("commStatus", count(comm.count)+" comm(s), unverified");
  renderByStatus("commByStatus", comm.statuses || {});
  // Followups
  setText("followCount", count(foll.count));
  setText("followStatus", count(foll.count)+" followup(s), unverified");
  renderByStatus("followByStatus", foll.statuses || {});
  // Ledger
  setText("ledgerCount", count(led.count));
  setText("ledgerNet", rupees(led.net_paise !== undefined ? led.net_paise : 0));
  setText("ledgerStatus", count(led.count)+" ledger entries, net "+rupees(led.net_paise !== undefined ? led.net_paise : 0));
  const ledgerKinds = led.kinds || {};
  renderByStatus("ledgerByKind", ledgerKinds);
  // 11-concept rows
  const leg11El = $("ledger11Rows");
  if (leg11El) {
    if (!leg11 || !leg11.proposed) {
      leg11El.innerHTML = '<div class="empty">No 11-concept — create order drafts, assertions, invoices and ledger entries to populate.</div>';
    } else {
      const rows = [
        ["proposed", leg11.proposed], ["quoted", leg11.quoted], ["contracted", leg11.contracted],
        ["invoiced_asserted", leg11.invoiced_asserted], ["invoiced_drafts", leg11.invoiced_drafts],
        ["invoices_issued", leg11.invoices_issued], ["payment_initiated", leg11.payment_initiated],
        ["payment_received_verified", leg11.payment_received_verified], ["payment_settled_synthetic", leg11.payment_settled_synthetic],
        ["refunds", leg11.refunds], ["expenses", leg11.expenses], ["fees", leg11.fees],
        ["ledger_net_unverified", leg11.ledger_net_unverified], ["net_realized_synthetic", leg11.net_realized_synthetic],
      ];
      leg11El.innerHTML = rows.map(([k,v])=>{
        const cnt = v && typeof v.count === "number" ? count(v.count) : "—";
        const amt = v && typeof v.amount_paise === "number" ? rupees(v.amount_paise) : "—";
        const ver = v && v.verification ? ' <small style="color:var(--muted)">'+escapeHtml(v.verification)+'</small>' : "";
        return '<div class="row"><div><b>'+escapeHtml(k)+'</b><small>count '+cnt+' · '+amt+ver+'</small></div><strong>'+amt+'</strong></div>';
      }).join("");
    }
  }
  // Invoices
  setText("invoiceCount", count(inv.count));
  setText("invoiceAmount", rupees(inv.amount_paise||0));
  setText("invoiceStatus", count(inv.count)+" invoice(s), unverified");
  renderByStatus("invoiceByStatus", inv.statuses || {});
  // Provider
  setText("providerCount", count(prov.count));
  setText("providerEventsCount", count(provevt.count));
  setText("providerCountNote", count(prov.count)+" order(s), "+(prov.verification||"sandbox_only"));
  renderByStatus("providerByStatus", prov.statuses || {});
  renderByStatus("providerByProvider", prov.providers || {});
  // Analytics
  setText("analyticsFunnel", ana.funnel ? count(ana.funnel.opportunities)+" opp, "+count(ana.funnel.customers)+" cust, "+count(ana.funnel.invoices)+" inv" : "No analytics");
  setText("funnelStatus", ana.funnel ? count(ana.funnel.opportunities)+" opp, "+count(ana.funnel.customers)+" cust, "+count(ana.funnel.invoices)+" inv" : "No analytics");
  setText("analyticsMrr", ana.mrr ? rupees(ana.mrr.total_paise||0)+" total, "+count(ana.mrr.months_with_income)+" months" : "No MRR");
  setText("mrrStatus", ana.mrr ? rupees(ana.mrr.total_paise||0)+" total, "+count(ana.mrr.months_with_income)+" months" : "No MRR");
  const buck = $("analyticsBuckets");
  if (buck) {
    const buckets = ana.mrr && ana.mrr.monthly_buckets ? ana.mrr.monthly_buckets : null;
    if (!buckets || Object.keys(buckets).length===0) buck.innerHTML = '<div class="empty">No monthly buckets — buckets are YYYY-MM ledger income, unverified.</div>';
    else {
      buck.innerHTML = Object.entries(buckets).sort().map(([k,v])=>'<div class="row"><div><b>'+escapeHtml(k)+'</b><small>ledger income (unverified)</small></div><strong>'+rupees(v)+'</strong></div>').join("");
    }
  }
  // Analytics — revenue intelligence (Phase 14)
  try {
    const rev = m.revenue_analytics || m.business_intelligence || null;
    const setRev = (id, val, noteId, note) => { setText(id, val); if (noteId) setText(noteId, note||""); };
    if (!rev || rev.empty_state) {
      // Empty honest state
      setRev("revTotalOpps", "0", "revTotalOppsNote", rev ? rev.total_opportunities.note : "Empty — no opportunities");
      setRev("revActiveOpps", "0");
      setRev("revAppsSubmitted", "0");
      setRev("revPropsAccepted", "0");
      setRev("revActiveCust", "0");
      setRev("revActiveProj", "0");
      setRev("revOrders", "0", "revOrdersNote", "Empty — no orders");
      setRev("revInvoices", "0", "revInvoicesNote", "Empty — no invoices");
      setRev("revOutstanding", "—", "revOutstandingNote", "Empty — no outstanding invoices");
      setRev("revVerified", "—", "revVerifiedNote", "Empty — no verified received payments");
      setRev("revExpenses", "—", "revExpensesNote", "Empty — no expenses");
      setRev("revFees", "—");
      setRev("revNet", "—", "revNetNote", "Empty — no realized income");
      setRev("revAvgProject", "—");
      setRev("revWorkload", "—", "revWorkloadNote", "Empty — no workload");
      setRev("revConversion", "—", "revConversionNote", "Empty — no conversion data");
      const emptyMsg = '<div class="empty">Empty — no data in this period. Create records and export a new snapshot. No fabrication.</div>';
      ["revenueChart","expenseChart","oppFunnelDetail","customerActivityDetail"].forEach(id=>{ const el=$(id); if(el) el.innerHTML=emptyMsg; });
      const incReal=$( "incomeRealized"), incUnv=$("incomeUnverified"), incProj=$("incomeProjected");
      if(incReal) incReal.textContent="—";
      if(incUnv) incUnv.textContent="—";
      if(incProj) incProj.textContent="—";
      if($("revFilterStatus")) $("revFilterStatus").textContent = "Showing all-time (no filter) — empty dataset, no attractive metrics fabricated.";
    } else {
      setRev("revTotalOpps", count(rev.total_opportunities.count), "revTotalOppsNote", rev.total_opportunities.verification + " — " + rev.total_opportunities.note);
      setRev("revActiveOpps", count(rev.active_opportunities.count));
      setRev("revAppsSubmitted", count(rev.applications_submitted.count));
      setRev("revPropsAccepted", count(rev.proposals_accepted.count));
      setRev("revActiveCust", count(rev.active_customers.count));
      setRev("revActiveProj", count(rev.active_projects.count));
      setRev("revOrders", count(rev.orders.count) + " · " + rupees(rev.orders.amount_paise), "revOrdersNote", "customer_orders " + rupees(rev.orders.amount_paise) + " + drafts " + rupees(rev.orders.order_drafts.amount_paise) + " (" + rev.orders.verification + ")");
      setRev("revInvoices", count(rev.invoices.count) + " · " + rupees(rev.invoices.amount_paise), "revInvoicesNote", "invoices " + rupees(rev.invoices.amount_paise) + " + drafts " + rupees(rev.invoices.drafts.amount_paise));
      setRev("revOutstanding", count(rev.outstanding_amounts.count) + " · " + rupees(rev.outstanding_amounts.amount_paise) + (rev.outstanding_amounts.overdue_count? " (overdue "+count(rev.outstanding_amounts.overdue_count)+")" : ""), "revOutstandingNote", rev.outstanding_amounts.verification + " — " + rev.outstanding_amounts.note);
      setRev("revVerified", count(rev.verified_received_payments.count) + " · " + rupees(rev.verified_received_payments.amount_paise), "revVerifiedNote", rev.verified_received_payments.verification + " — " + rev.verified_received_payments.note);
      setRev("revExpenses", count(rev.expenses.count) + " · " + rupees(rev.expenses.amount_paise) + (rev.expenses.ledger_expense_paise ? " + ledger " + rupees(rev.expenses.ledger_expense_paise) : ""), "revExpensesNote", rev.expenses.verification);
      setRev("revFees", rupees(rev.platform_fees.amount_paise) + " · " + count(rev.platform_fees.count), null, null);
      setRev("revNet", rupees(rev.net_realized_income.amount_paise), "revNetNote", rev.net_realized_income.verification + " — " + rev.net_realized_income.note);
      setRev("revAvgProject", rupees(rev.average_project_value.amount_paise), null, null);
      const wl = rev.workload || {};
      const tc = rev.task_completion || {};
      setRev("revWorkload", (wl.queued_tasks!==undefined? count(wl.queued_tasks)+" queued" : "") + (wl.project_tasks? " · " + count(wl.project_tasks.total)+" tasks, "+count(wl.project_tasks.completed)+" completed ("+Math.round((wl.project_tasks.completion_rate||0)*100)+"% )" : ""), "revWorkloadNote", "ledger_net " + rupees(wl.ledger_net_paise||0));
      const conv = rev.conversion_rate || {};
      let convText = [];
      if(conv.opportunities) convText.push("opps vetted "+Math.round((conv.opportunities.vetted_rate||0)*100)+"%");
      if(conv.customers) convText.push("cust converted "+Math.round((conv.customers.converted_over_lead||0)*100)+"%");
      if(conv.proposals) convText.push("props accepted "+Math.round((conv.proposals.accepted_over_total||conv.proposals.accepted_rate||0)*100)+"%");
      setRev("revConversion", convText.join(" · ") || "—", "revConversionNote", "real ratios — 0 if denominator 0");
      // income separation
      const incSep = rev.income_separation || {};
      setText("incomeRealized", incSep.realized_income ? rupees(incSep.realized_income.amount_paise) + " (" + incSep.realized_income.verification + ")" : "—");
      setText("incomeUnverified", incSep.unverified_income ? rupees(incSep.unverified_income.amount_paise) + " (" + incSep.unverified_income.verification + " — ledger)" : "—");
      setText("incomeProjected", incSep.projected_revenue ? rupees(incSep.projected_revenue.amount_paise) + " (" + incSep.projected_revenue.verification + " — not revenue)" : "—");
      // helper to render monthly buckets as rows with simple bar
      function renderBuckets(containerId, buckets, verification, emptyMsg){
        const el=$(containerId);
        if(!el) return;
        if(!buckets || Object.keys(buckets).length===0){
          el.innerHTML='<div class="empty">'+emptyMsg+'</div>';
          return;
        }
        // Apply time filter if set
        const fromVal = $("revFrom") ? $("revFrom").value : "";
        const toVal = $("revTo") ? $("revTo").value : "";
        let entries = Object.entries(buckets).sort();
        // Filter by YYYY-MM if inputs have values
        function ymToVal(ym){ if(!ym) return null; const parts=ym.split("-"); return parts[0]+"-"+parts[1]; }
        const fv = ymToVal(fromVal), tv = ymToVal(toVal);
        if(fv) entries = entries.filter(([k])=> k >= fv);
        if(tv) entries = entries.filter(([k])=> k <= tv);
        if(entries.length===0){
          el.innerHTML='<div class="empty">No data in selected time range — honest empty state.</div>';
          if($("revFilterStatus")) $("revFilterStatus").textContent = "Filtered "+fv+" to "+tv+" — empty (no fabrication).";
          return;
        }
        const max = Math.max(...entries.map(([,v])=>v), 1);
        el.innerHTML = entries.map(([k,v])=>{
          const pct = Math.round((v/max)*100);
          return '<div class="row"><div><b>'+escapeHtml(k)+'</b><small>'+escapeHtml(verification)+' · '+rupees(v)+'<br><span style="display:inline-block;height:6px;background:var(--accent);width:'+pct+'%;border-radius:3px"></span> '+pct+'%</small></div><strong>'+rupees(v)+'</strong></div>';
        }).join("");
        if($("revFilterStatus")) $("revFilterStatus").textContent = fv||tv ? "Filtered "+(fv||"...")+" to "+(tv||"...")+" — "+entries.length+" month(s) shown." : "Showing all-time — "+entries.length+" month(s).";
      }
      renderBuckets("revenueChart", Object.assign({}, rev.revenue_charts.realized_monthly, rev.revenue_charts.unverified_monthly, rev.revenue_charts.projected_monthly), "realized=verified, unverified=ledger, projected=draft", "No revenue months — honest empty state, never fabricated.");
      // For revenue chart we want separate layers: show realized vs unverified vs projected separately? For simplicity we combine but label
      // Instead, render separate sections for realized, unverified, projected if needed
      // We'll just render combined for now; detailed separation is in incomeSeparation
      // Expense buckets
      renderBuckets("expenseChart", rev.expense_charts.monthly, "verified expense_records", "No expense months — honest empty state.");
      // Opportunity funnel
      const oppEl=$("oppFunnelDetail");
      if(oppEl){
        const f = rev.opportunity_funnel || {};
        if(f.empty_state) oppEl.innerHTML='<div class="empty">Empty — no opportunities in this period.</div>';
        else {
          const rows = Object.entries(f.by_status||{}).map(([k,v])=>'<div class="row"><div><b>'+escapeHtml(k)+'</b><small>funnel stage</small></div><strong>'+count(v)+'</strong></div>').join("");
          oppEl.innerHTML = rows + '<div class="muted" style="margin-top:8px">Active: '+count(f.active)+' · '+escapeHtml(f.verification||"")+'</div>';
        }
      }
      // Customer activity
      const caEl=$("customerActivityDetail");
      if(caEl){
        const ca = rev.customer_activity || {};
        if(ca.empty_state) caEl.innerHTML='<div class="empty">Empty — no customers in this period.</div>';
        else {
          const byStatus = Object.entries(ca.customers_by_status||{}).map(([k,v])=>k+":"+count(v)).join(" ");
          caEl.innerHTML = '<div class="row"><div><b>Customers by status</b><small>'+escapeHtml(byStatus)+'</small></div><strong>'+count(ca.total_customers)+'</strong></div>' +
            '<div class="row"><div><b>Communications</b><small>in period</small></div><strong>'+count(ca.communications_in_period)+'</strong></div>' +
            '<div class="row"><div><b>Orders</b><small>in period</small></div><strong>'+count(ca.orders_in_period)+'</strong></div>' +
            '<div class="row"><div><b>Followups</b><small>pending? in period</small></div><strong>'+count(ca.followups_in_period)+'</strong></div>' +
            '<div class="muted" style="margin-top:8px">'+escapeHtml(ca.verification||"")+'</div>';
        }
      }
    }
  } catch(e){ console.error("rev render error", e); }

  // AI
  setText("aiStatus", count(aia.count)+" approval(s), gated"+(aia.pending_expired_virtual ? " · "+count(aia.pending_expired_virtual)+" virtual expired" : ""));
  renderByStatus("aiByStatus", aia.statuses || {});
  const emeState = eme.stopped ? "STOPPED: "+(eme.reason||"emergency") : (eme.stopped===false ? "clear: consequential allowed if approved" : "No emergency");
  setText("emergencyState", emeState);
  setText("emergencyStatus", emeState);
  // Queue / Workflows
  setText("queueCount", count(w.queued_tasks));
  setText("queueStatus", count(w.queued_tasks)+" queued or leased");
  setText("workflowCount", count(w.task_workflows));
  // Diagnostics
  setText("snapshotGenerated", snapshot.exported_at || "Timestamp unavailable");
  setText("snapshotSchema", count(m.schema_version));
  setText("snapshotMode", "Authenticated, account-scoped, read-only");
  setText("snapshotFileName", snapshot._fileName || "Loaded from file input");
  setText("diagSchema", count(m.schema_version));
  setText("diagReadonly", "Snapshot data is read into this page only and is not uploaded.");
  $("snapshotStatus").textContent = "Snapshot loaded — "+count(m.schema_version)+" schema, read-only snapshot.";
  document.querySelector(".status").textContent = "READ-ONLY SNAPSHOT";
  const em = $("overviewEmpty"); if(em) em.style.display="none";
  const foot = $("dashboardFootnote"); if(foot) foot.textContent = "Snapshot data only; no live connection. Loaded at "+(snapshot.exported_at||"unknown")+".";
}

function renderEmpty(){
  currentSnapshot=null; currentMetrics=null;
  ["metricPipeline","metricInvoices","metricObservations","oppCount","oppAmount","svcCount","svcAmount","portfolioCount","prodCount","prodAmount","custCount","commCount","followCount","ledgerCount","ledgerNet","invoiceCount","invoiceAmount","providerCount","providerEventsCount","aiCount","workOpps","workServices","workProducts","workCustomers","workLedgerNet","workInvoicesIssued","workProvider","workAI","workFunnel","analyticsFunnel","analyticsMrr","queueCount","workflowCount","revTotalOpps","revActiveOpps","revAppsSubmitted","revPropsAccepted","revActiveCust","revActiveProj","revOrders","revInvoices","revOutstanding","revVerified","revExpenses","revFees","revNet","revAvgProject","revWorkload","revConversion","incomeRealized","incomeUnverified","incomeProjected"].forEach(id=>{
    const el=$(id); if(el) el.textContent="—";
  });
  ["metricIncome"].forEach(id=>{ const el=$(id); if(el) el.textContent="Not available"; });
  ["metricPipelineNote","metricInvoicesNote","metricObservationsNote","pipelineStatus","ledger11Status","providerStatus","incomeStatus","oppStatus","svcStatus","portfolioStatus","prodStatus","custStatus","commStatus","followStatus","ledgerStatus","invoiceStatus","providerCountNote","aiStatus","emergencyStatus","emergencyState","funnelStatus","mrrStatus","queueStatus","overviewMode","revTotalOppsNote","revOrdersNote","revInvoicesNote","revOutstandingNote","revVerifiedNote","revExpensesNote","revNetNote","revWorkloadNote","revConversionNote"].forEach(id=>{
    const el=$(id); if(el) el.textContent="No snapshot loaded";
  });
  ["oppByStatus","svcByStatus","prodByStatus","custByStatus","commByStatus","followByStatus","ledgerByKind","invoiceByStatus","providerByStatus","providerByProvider","aiByStatus","analyticsBuckets","ledger11Rows","revenueChart","expenseChart","oppFunnelDetail","customerActivityDetail","incomeSeparation"].forEach(id=>{
    const el=$(id); if(el) el.innerHTML='<div class="empty">No snapshot loaded — statuses appear after you create records and export a snapshot.</div>';
  });
  const snapGen=$("snapshotGenerated"); if(snapGen) snapGen.textContent="Not loaded";
  const snapSch=$("snapshotSchema"); if(snapSch) snapSch.textContent="Not loaded";
  const snapMode=$("snapshotMode"); if(snapMode) snapMode.textContent="Waiting for a snapshot";
  const snapFile=$("snapshotFileName"); if(snapFile) snapFile.textContent="None loaded";
  const diagSch=$("diagSchema"); if(diagSch) diagSch.textContent="—";
  $("snapshotStatus").textContent="No snapshot loaded. Your browser has not received any account data.";
  document.querySelector(".status").textContent="NO DATA LOADED";
  const foot=$("dashboardFootnote"); if(foot) foot.textContent="No account data loaded · snapshot data is read into this page only and is not uploaded. Theme follows system, toggle persists locally.";
  const em=$("overviewEmpty"); if(em){ em.style.display="block"; em.textContent="No snapshot loaded — metrics show honest empty states. Use the tracker CLI to create records, then export a snapshot."; }
  const overviewNote=$("overviewMode"); if(overviewNote) overviewNote.textContent="Waiting for a snapshot. No fabricated data is shown.";
}

function handleFile(file){
  const statusEl=$("snapshotStatus");
  if (!file) return;
  statusEl.textContent="Loading snapshot...";
  if (file.size > 1048576) { statusEl.textContent="Snapshot exceeds 1 MB — refusing to load."; statusEl.className="status error"; setTimeout(()=>statusEl.className="status",3000); return; }
  const reader = new FileReader();
  reader.onerror = ()=>{ statusEl.textContent="Could not read file."; statusEl.className="status error"; };
  reader.onload = ()=>{
    try{
      const text = reader.result;
      const data = JSON.parse(text);
      data._fileName = file.name;
      render(data);
      currentSnapshot=data;
      statusEl.className="status";
    } catch(e){
      statusEl.textContent = e && e.message ? e.message : "Could not load snapshot.";
      statusEl.className="status error";
      const inp=$("snapshotFile"); if(inp) inp.value="";
    }
  };
  reader.readAsText(file);
}

function initSnapshotLoader(){
  const inp=$("snapshotFile"), btn=$("clearSnapshot");
  if (inp) inp.addEventListener("change", (e)=>{
    const file = e.target.files && e.target.files[0];
    if (file) handleFile(file);
  });
  if (btn) btn.addEventListener("click", ()=>{
    renderEmpty();
    if (inp) inp.value="";
    history.replaceState(null,"","#/overview");
    updateRoute();
  });
}

function initRevenueHandlers(){
  const btn=$("revFilterBtn"), clear=$("revClearFilter"), exp=$("revExportBtn"), expJ=$("revExportJsonBtn");
  if(btn) btn.addEventListener("click", ()=>{
    if(currentSnapshot) render(currentSnapshot);
    else { const el=$("revFilterStatus"); if(el) el.textContent="No snapshot — load a snapshot first."; }
  });
  if(clear) clear.addEventListener("click", ()=>{
    const f=$("revFrom"), t=$("revTo");
    if(f) f.value=""; if(t) t.value="";
    if(currentSnapshot) render(currentSnapshot);
    const el=$("revFilterStatus"); if(el) el.textContent="Showing all-time (no filter).";
  });
  function download(filename, text, mime){
    const blob=new Blob([text], {type: mime});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url; a.download=filename; a.click();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }
  if(exp) exp.addEventListener("click", ()=>{
    if(!currentMetrics || !currentMetrics.revenue_analytics){ alert("No revenue data — load a snapshot first."); return; }
    try{
      const rev=currentMetrics.revenue_analytics;
      // Build simple CSV client-side from rev (mirrors server export)
      let csv="metric,period_from,period_to,value_paise_or_count,verification,note\n";
      const f=rev.from_ts||0, t=rev.to_ts||4102444800;
      const add=(k,v,ver,note)=>{ csv+=k+","+f+","+t+","+v+","+ver+","+(note||"").replace(/,/g,";")+"\n"; };
      add("total_opportunities", rev.total_opportunities.count, "unverified", rev.total_opportunities.note);
      add("active_opportunities", rev.active_opportunities.count, "unverified", "");
      add("applications_submitted", rev.applications_submitted.count, "unverified", "");
      add("proposals_accepted", rev.proposals_accepted.count, "unverified", "");
      add("active_customers", rev.active_customers.count, "unverified", "");
      add("active_projects", rev.active_projects.count, "unverified", "");
      add("orders_count", rev.orders.count, "unverified", "");
      add("orders_amount", rev.orders.amount_paise, "unverified", "INR");
      add("invoices_count", rev.invoices.count, "unverified", "");
      add("invoices_amount", rev.invoices.amount_paise, "unverified", "INR");
      add("outstanding_count", rev.outstanding_amounts.count, "unverified", "");
      add("outstanding_amount", rev.outstanding_amounts.amount_paise, "unverified", "INR");
      add("verified_received_count", rev.verified_received_payments.count, "verified", "");
      add("verified_received_amount", rev.verified_received_payments.amount_paise, "verified", "INR");
      add("expenses_amount", rev.expenses.amount_paise, "verified", "");
      add("platform_fees", rev.platform_fees.amount_paise, "verified", "");
      add("net_realized", rev.net_realized_income.amount_paise, "verified", "");
      Object.entries(rev.monthly_income.realized||{}).forEach(([k,v])=> add("monthly_income_realized_"+k, v, "verified", "UTC bucket"));
      Object.entries(rev.monthly_income.unverified||{}).forEach(([k,v])=> add("monthly_income_unverified_"+k, v, "unverified", ""));
      Object.entries(rev.monthly_expenses.monthly||{}).forEach(([k,v])=> add("monthly_expenses_"+k, v, "verified", ""));
      if(rev.empty_state) add("empty_state","true","none","Empty — no data");
      if(csv.length>1000000) { alert("Export too large"); return; }
      download("revenue-report.csv", csv, "text/csv");
    } catch(e){ alert("Export failed: "+(e.message||e)); }
  });
  if(expJ) expJ.addEventListener("click", ()=>{
    if(!currentMetrics || !currentMetrics.revenue_analytics){ alert("No revenue data"); return; }
    const text=JSON.stringify(currentMetrics.revenue_analytics, null, 2);
    if(text.length>1000000) { alert("Export too large"); return; }
    const blob=new Blob([text], {type:"application/json"});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url; a.download="revenue-report.json"; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
}
document.addEventListener("DOMContentLoaded", ()=>{
  initTheme();
  initNavToggle();
  initRouter();
  initSnapshotLoader();
  initRevenueHandlers();
  renderEmpty();
});
