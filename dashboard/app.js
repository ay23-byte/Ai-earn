// Earn OS SPA — vanilla hash router, authenticated same-origin workspace API, and snapshot fallback.
// Premium SaaS polish: reusable components, notifications, validation, charts, a11y.
"use strict";
const $ = (id) => document.getElementById(id);
const count = (v) => Number.isSafeInteger(v) && v >= 0 ? String(v) : "0";
const rupees = (v) => {
  const n = Number.isSafeInteger(v) && v >= 0 ? v : 0;
  try { return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(n / 100); } catch (_) { return "INR " + (n/100).toFixed(2); }
};
// Core navigation — 17 premium SaaS sections (meets Phase 15 spec)
const ROUTES = ["dashboard","opportunities","services","proposals","customers","projects","tasks","ai-assistant","digital-products","orders","invoices","payments","expenses","analytics","integrations","settings","security"];
// Aliases for backward compatibility (old hashes still work)
const ALIASES = { "overview":"dashboard", "portfolio":"proposals", "products":"digital-products", "communications":"customers", "followups":"tasks", "ledger":"expenses", "provider":"payments", "ai-approvals":"ai-assistant", "connectors":"integrations", "queue":"tasks", "workflows":"projects", "diagnostics":"security" };
let currentSnapshot = null;
let currentMetrics = null;

// Theme
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
      try{ if (!localStorage.getItem("earn_os_theme")) applyTheme(null); }catch(_){}
    });
  }
}
function initNavToggle(){
  const btn = $("navToggle"), nav = $("primaryNav");
  if (!btn || !nav) return;
  btn.addEventListener("click", ()=>{
    const open = nav.classList.toggle("open");
    btn.setAttribute("aria-expanded", String(open));
  });
}
function normalizeRoute(hash){
  const raw = (hash || "").replace(/^#\/?/, "").split("?")[0].split("/")[0].trim().toLowerCase();
  if (ROUTES.includes(raw)) return raw;
  if (ALIASES[raw] && ROUTES.includes(ALIASES[raw])) return ALIASES[raw];
  if (!raw) return "dashboard";
  return "dashboard";
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

// Reusable components
function escapeHtml(s){ return String(s).replace(/[&<>\"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
function setText(id, text){ const el=$(id); if(el) el.textContent=text; }
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
function showToast(message, type){
  const cont = $("toastContainer");
  if(!cont) return;
  const toast = document.createElement("div");
  toast.className = "toast " + (type||"");
  toast.setAttribute("role","status");
  const icon = type==="success" ? "✓" : type==="error" ? "✕" : "◯";
  toast.innerHTML = '<span aria-hidden="true">'+icon+'</span><span>'+escapeHtml(message)+'</span><button class="toast-close" aria-label="Dismiss">×</button>';
  const close = ()=>{ toast.style.opacity="0"; toast.style.transform="translateX(12px)"; setTimeout(()=>toast.remove(),200); };
  toast.querySelector(".toast-close").addEventListener("click", close);
  cont.appendChild(toast);
  setTimeout(close, 4000);
}
function showLoading(show){
  const ov = $("loadingOverlay");
  if(!ov) return;
  if(show){ ov.removeAttribute("hidden"); ov.setAttribute("aria-hidden","false"); }
  else { ov.setAttribute("hidden",""); ov.setAttribute("aria-hidden","true"); }
}
function renderChart(containerId, buckets, opts){
  const el=$(containerId);
  if(!el) return;
  const verification = opts && opts.verification ? opts.verification : "";
  const emptyMsg = opts && opts.emptyMsg ? opts.emptyMsg : "No data — honest empty state.";
  if(!buckets || Object.keys(buckets).length===0){
    el.innerHTML='<div class="empty">'+escapeHtml(emptyMsg)+'</div>';
    return;
  }
  // Filter by time if inputs exist
  const fromVal = $("revFrom") ? $("revFrom").value : "";
  const toVal = $("revTo") ? $("revTo").value : "";
  let entries = Object.entries(buckets).sort();
  const fv = fromVal ? fromVal : null;
  const tv = toVal ? toVal : null;
  if(fv) entries = entries.filter(([k])=> k >= fv);
  if(tv) entries = entries.filter(([k])=> k <= tv);
  if(entries.length===0){
    el.innerHTML='<div class="empty">No data in selected time range — honest empty state.</div>';
    return;
  }
  const max = Math.max(...entries.map(([,v])=>v), 1);
  el.innerHTML = entries.map(([k,v])=>{
    const pct = Math.round((v/max)*100);
    return '<div class="bar-row"><span>'+escapeHtml(k)+'</span><div class="bar-track"><div class="bar-fill" style="width:'+pct+'%"></div></div><strong>'+rupees(v)+'</strong></div>';
  }).join("") + '<div class="muted" style="margin-top:8px">'+escapeHtml(verification)+' · '+entries.length+' month(s)</div>';
}

// Main render
function render(snapshot, source){
  if (!snapshot || snapshot.format !== "ai-earn-dashboard-snapshot" || snapshot.version !== 1 || !snapshot.metrics || snapshot.metrics.read_only !== true) throw Error("Unsupported snapshot. Expected format ai-earn-dashboard-snapshot version 1 with read_only metrics.");
  const m = snapshot.metrics;
  const w = m.work||{}, c=m.commercial||{}, p=m.asserted_pipeline||{}, o=m.payment_observations||{}, inc=m.income||{}, opp=m.opportunities||{}, svc=m.services||{}, pf=m.portfolio||{}, prod=m.products||{}, cust=m.customers||{}, comm=m.communications||{}, led=m.ledger||{}, foll=m.followups||{}, inv=m.invoices||{}, leg11=m.ledger_11||{}, prov=m.provider_orders||{}, provevt=m.provider_events||{}, aia=m.ai_approvals||{}, eme=m.ai_emergency||{}, ana=m.analytics||{}, rev=m.revenue_analytics||m.business_intelligence||null;
  currentMetrics = m;

  // Dashboard hero
  const dashTotal = rev ? rev.total_opportunities : null;
  setText("dashTotalOpps", rev ? count(rev.total_opportunities.count) : count(opp.count));
  setText("dashTotalOppsNote", rev ? rev.total_opportunities.verification : (opp.verification||"unverified"));
  setText("dashActiveProj", rev ? count(rev.active_projects.count) : "—");
  setText("dashActiveProjNote", rev ? rev.active_projects.note : "");
  const verifiedAmt = rev ? rev.verified_received_payments.amount_paise : (inc.verified_amount_paise||0);
  const verifiedNote = rev ? rev.verified_received_payments.verification : inc.status;
  setText("dashVerified", rev ? rupees(verifiedAmt) : (inc.status==="not_available_no_verified_payment_ledger" ? "Not available" : rupees(verifiedAmt)));
  setText("dashVerifiedNote", verifiedNote||"");
  const netAmt = rev ? rev.net_realized_income.amount_paise : (leg11.net_realized_synthetic ? leg11.net_realized_synthetic.amount_paise : 0);
  setText("dashNet", rupees(netAmt));
  setText("dashNetNote", rev ? rev.net_realized_income.verification : "");
  setText("dashOutstanding", rev ? rupees(rev.outstanding_amounts.amount_paise) + " · " + count(rev.outstanding_amounts.count) : "—");
  setText("dashOutstandingNote", rev ? rev.outstanding_amounts.note : "");
  setText("dashActiveCust", rev ? count(rev.active_customers.count) : count(cust.count));
  const tc = rev ? rev.task_completion : null;
  setText("dashTasks", tc ? Math.round((tc.completion_rate||0)*100)+"%" : "—");
  setText("dashTasksNote", tc ? count(tc.completed)+"/"+count(tc.total) : "");
  setText("dashMrr", ana.mrr ? rupees(ana.mrr.total_paise||0) : "—");
  setText("dashMrrNote", ana.mrr ? count(ana.mrr.months_with_income)+" months" : "No MRR");
  if(rev && rev.income_separation){
    setText("dashRealized", rupees(rev.income_separation.realized_income.amount_paise));
    setText("dashUnverified", rupees(rev.income_separation.unverified_income.amount_paise));
    setText("dashProjected", rupees(rev.income_separation.projected_revenue.amount_paise));
  }
  // Streams
  setText("workOpps", count(opp.count));
  setText("workServices", count(svc.count));
  setText("workProducts", count(prod.count));
  setText("workCustomers", count(cust.count));
  setText("workLedgerNet", rupees(led.net_paise !== undefined ? led.net_paise : 0));
  setText("workInvoicesIssued", count(inv.count));
  setText("workProvider", count(prov.count));
  setText("workAI", count(aia.count));
  setText("workFunnel", ana.funnel ? (count(ana.funnel.opportunities)+"/"+count(ana.funnel.customers)+"/"+count(ana.funnel.invoices)) : "—");
  // Hide empty notice if data present
  const em = $("overviewEmpty"); if(em) em.style.display = (m.schema_version && (opp.count||svc.count||prod.count)) ? "none" : "block";

  // Opportunities
  setText("oppCount", count(opp.count));
  setText("oppActiveCount", rev ? count(rev.active_opportunities.count) : count(opp.count));
  setText("oppAmount", rupees(opp.amount_paise||0));
  setText("oppStatus", count(opp.count)+" discovered (unverified)");
  renderByStatus("oppByStatus", opp.statuses);
  if(rev && rev.opportunity_funnel && !rev.opportunity_funnel.empty_state){
    renderChart("oppChart", rev.opportunity_funnel.by_status, {verification:"funnel", emptyMsg:"No opportunities"});
  } else {
    const oc=$("oppChart"); if(oc) oc.innerHTML='<div class="empty">No chart — create opportunities to see funnel.</div>';
  }

  // Services
  setText("svcCount", count(svc.count));
  setText("svcAmount", rupees(svc.amount_paise||0));
  setText("svcStatus", count(svc.count)+" services");
  renderByStatus("svcByStatus", svc.statuses);

  // Proposals
  const propSubmitted = rev ? rev.applications_submitted.count : 0;
  const propAccepted = rev ? rev.proposals_accepted.count : 0;
  setText("propSubmitted", count(propSubmitted));
  setText("propAccepted", count(propAccepted));
  const conv = rev ? rev.conversion_rate.proposals : null;
  setText("propConversion", conv ? Math.round((conv.accepted_over_total||conv.accepted_rate||0)*100)+"%" : "—");
  setText("propStatus", rev ? count(propSubmitted)+" submitted" : "No snapshot");
  renderByStatus("propByStatus", conv ? conv.by_status : {});

  // Customers
  setText("custCount", count(cust.count));
  setText("custActive", rev ? count(rev.active_customers.count) : count(cust.count));
  setText("custStatus", count(cust.count)+" leads");
  renderByStatus("custByStatus", cust.statuses||{});
  if(rev && rev.customer_activity && !rev.customer_activity.empty_state){
    const ca=rev.customer_activity;
    const caEl=$("customerActivityDetail");
    if(caEl){
      caEl.innerHTML = '<div class="row"><div><b>Customers by status</b><small>'+escapeHtml(Object.entries(ca.customers_by_status||{}).map(([k,v])=>k+":"+count(v)).join(" "))+'</small></div><strong>'+count(ca.total_customers)+'</strong></div>' +
      '<div class="row"><div><b>Communications</b><small>in period</small></div><strong>'+count(ca.communications_in_period)+'</strong></div>' +
      '<div class="row"><div><b>Orders</b><small>in period</small></div><strong>'+count(ca.orders_in_period)+'</strong></div>';
    }
  }

  // Projects
  setText("projActive", rev ? count(rev.active_projects.count) : "—");
  setText("projTotal", rev ? count(rev.active_projects.count) : "—");
  const projEl=$("projByCustomer");
  if(projEl){
    if(rev && !rev.active_projects.count) projEl.innerHTML='<div class="empty">No projects — link customer_projects via tracker.</div>';
    else projEl.innerHTML='<div class="row"><div><b>Active projects</b><small>customer-linked</small></div><strong>'+count(rev?rev.active_projects.count:0)+'</strong></div>';
  }

  // Tasks
  const wl = rev ? rev.workload : null;
  setText("taskQueued", wl ? count(wl.queued_tasks) : "—");
  setText("taskProjCount", wl && wl.project_tasks ? count(wl.project_tasks.total) : "—");
  const tc2 = rev ? rev.task_completion : null;
  setText("taskCompletion", tc2 ? Math.round((tc2.completion_rate||0)*100)+"%" : "—");
  renderByStatus("taskByStatus", wl && wl.project_tasks ? wl.project_tasks.by_status : {});
  if(wl && wl.project_tasks) renderChart("taskChart", wl.project_tasks.by_status, {verification:"tasks by status", emptyMsg:"No tasks"});

  // AI Assistant
  setText("aiCount", count(aia.count));
  setText("aiStatus", count(aia.count)+" approvals");
  renderByStatus("aiByStatus", aia.statuses||{});
  const emeState = eme.stopped ? "STOPPED: "+(eme.reason||"emergency") : (eme.stopped===false ? "clear" : "No emergency");
  setText("emergencyState", emeState);
  setText("emergencyStatus", emeState);

  // Digital Products
  setText("prodCount", count(prod.count));
  setText("prodAmount", rupees(prod.amount_paise||0));
  setText("prodStatus", count(prod.count)+" products");
  renderByStatus("prodByStatus", prod.statuses);

  // Orders
  const orders = rev ? rev.orders : null;
  setText("ordersCount", orders ? count(orders.count) : "—");
  setText("ordersAmount", orders ? rupees(orders.amount_paise) : "—");
  setText("orderDrafts", orders ? count(orders.order_drafts.count)+" · "+rupees(orders.order_drafts.amount_paise) : "—");
  setText("ordersStatus", orders ? count(orders.count)+" orders" : "No snapshot");
  renderByStatus("ordersByStatus", orders ? {draft:orders.order_drafts, confirmed:{count:orders.count}} : {});

  // Invoices
  setText("invoiceCount", count(inv.count));
  setText("invoiceAmount", rupees(inv.amount_paise||0));
  setText("invoiceStatus", count(inv.count)+" invoices");
  const outAmt = rev ? rev.outstanding_amounts : null;
  setText("invoiceOutstanding", outAmt ? rupees(outAmt.amount_paise) : "—");
  renderByStatus("invoiceByStatus", inv.statuses||{});

  // Payments
  setText("payVerified", rev ? rupees(rev.verified_received_payments.amount_paise) + " · " + count(rev.verified_received_payments.count) : "—");
  setText("payVerifiedNote", rev ? rev.verified_received_payments.verification : "");
  setText("providerCount", count(prov.count));
  setText("providerEventsCount", count(provevt.count));
  setText("providerCountNote", count(prov.count)+" orders");
  renderByStatus("providerByStatus", prov.statuses||{});
  renderByStatus("providerByProvider", prov.providers||{});
  const leg11El = $("ledger11Rows");
  if (leg11El) {
    if (!leg11 || !leg11.proposed) {
      leg11El.innerHTML = '<div class="empty">No 11-concept — create order drafts, assertions, invoices and ledger entries.</div>';
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

  // Expenses
  const exp = rev ? rev.expenses : null;
  setText("expCount", exp ? count(exp.count) : "—");
  setText("expAmount", exp ? rupees(exp.amount_paise) : "—");
  setText("expFees", rev ? rupees(rev.platform_fees.amount_paise) : "—");
  setText("expStatus", exp ? count(exp.count)+" expenses" : "No snapshot");
  setText("ledgerNet", rupees(led.net_paise !== undefined ? led.net_paise : 0));
  renderByStatus("ledgerByKind", led.kinds||{});
  if(rev) renderChart("expChart", rev.expense_charts.monthly, {verification:"verified expenses", emptyMsg:"No expenses"});

  // Analytics (MRR + revenue)
  setText("analyticsFunnel", ana.funnel ? count(ana.funnel.opportunities)+" opp, "+count(ana.funnel.customers)+" cust" : "No analytics");
  setText("funnelStatus", ana.funnel ? count(ana.funnel.opportunities)+" opp, "+count(ana.funnel.customers)+" cust" : "No analytics");
  setText("analyticsMrr", ana.mrr ? rupees(ana.mrr.total_paise||0)+" total" : "No MRR");
  setText("mrrStatus", ana.mrr ? rupees(ana.mrr.total_paise||0) : "No MRR");
  const buck = $("analyticsBuckets");
  if (buck) {
    const buckets = ana.mrr && ana.mrr.monthly_buckets ? ana.mrr.monthly_buckets : null;
    if (!buckets || Object.keys(buckets).length===0) buck.innerHTML = '<div class="empty">No monthly buckets — buckets are YYYY-MM ledger income, unverified.</div>';
    else buck.innerHTML = Object.entries(buckets).sort().map(([k,v])=>'<div class="row"><div><b>'+escapeHtml(k)+'</b><small>ledger income (unverified)</small></div><strong>'+rupees(v)+'</strong></div>').join("");
  }
  // Revenue analytics panel
  if(rev){
    setText("revTotalOpps", count(rev.total_opportunities.count));
    setText("revTotalOppsNote", rev.total_opportunities.verification);
    setText("revActiveOpps", count(rev.active_opportunities.count));
    setText("revAppsSubmitted", count(rev.applications_submitted.count));
    setText("revPropsAccepted", count(rev.proposals_accepted.count));
    setText("revActiveCust", count(rev.active_customers.count));
    setText("revActiveProj", count(rev.active_projects.count));
    setText("revOrders", count(rev.orders.count)+" · "+rupees(rev.orders.amount_paise));
    setText("revOrdersNote", "drafts "+rupees(rev.orders.order_drafts.amount_paise));
    setText("revInvoices", count(rev.invoices.count)+" · "+rupees(rev.invoices.amount_paise));
    setText("revOutstanding", rupees(rev.outstanding_amounts.amount_paise));
    setText("revVerified", rupees(rev.verified_received_payments.amount_paise));
    setText("revExpenses", rupees(rev.expenses.amount_paise));
    setText("revFees", rupees(rev.platform_fees.amount_paise));
    setText("revNet", rupees(rev.net_realized_income.amount_paise));
    setText("revAvgProject", rupees(rev.average_project_value.amount_paise));
    const wl2 = rev.workload;
    setText("revWorkload", wl2 && wl2.project_tasks ? Math.round((wl2.project_tasks.completion_rate||0)*100)+"%" : "—");
    const conv2 = rev.conversion_rate;
    let convText = [];
    if(conv2.opportunities) convText.push("opps "+Math.round((conv2.opportunities.vetted_rate||0)*100)+"%");
    if(conv2.customers) convText.push("cust "+Math.round((conv2.customers.converted_over_lead||0)*100)+"%");
    setText("revConversion", convText.join(" · ")||"—");
    setText("incomeRealized", rupees(rev.income_separation.realized_income.amount_paise));
    setText("incomeUnverified", rupees(rev.income_separation.unverified_income.amount_paise));
    setText("incomeProjected", rupees(rev.income_separation.projected_revenue.amount_paise));
    // Charts
    const realized = rev.revenue_charts.realized_monthly || {};
    const unverified = rev.revenue_charts.unverified_monthly || {};
    const projected = rev.revenue_charts.projected_monthly || {};
    const combined = Object.assign({}, realized, unverified, projected);
    // For revenue chart, show realized vs unverified vs projected as separate bars? We'll show combined with label
    renderChart("revenueChart", combined, {verification:"realized=verified, unverified=ledger, projected=draft", emptyMsg:"No revenue months"});
    renderChart("expenseChart", rev.expense_charts.monthly, {verification:"verified expenses", emptyMsg:"No expense months"});
    // Funnel detail
    const oppF = rev.opportunity_funnel;
    const oppEl2=$("oppFunnelDetail");
    if(oppEl2){
      if(oppF.empty_state) oppEl2.innerHTML='<div class="empty">Empty — no opportunities.</div>';
      else oppEl2.innerHTML = Object.entries(oppF.by_status||{}).map(([k,v])=>'<div class="row"><div><b>'+escapeHtml(k)+'</b></div><strong>'+count(v)+'</strong></div>').join("");
    }
    const caEl2=$("customerActivityDetail");
    if(caEl2){
      const ca=rev.customer_activity;
      if(ca.empty_state) caEl2.innerHTML='<div class="empty">Empty — no customers.</div>';
      else caEl2.innerHTML = '<div class="row"><div><b>Customers</b><small>'+escapeHtml(Object.entries(ca.customers_by_status||{}).map(([k,v])=>k+":"+count(v)).join(" "))+'</small></div><strong>'+count(ca.total_customers)+'</strong></div>';
    }
  }

  // Integrations
  const intEl=$("integrationStatus");
  if(intEl){
    if(!prov.count && !aia.count) intEl.innerHTML='<div class="empty">No integrations — connectors via tracker 46–48.</div>';
    else intEl.innerHTML='<div class="row"><div><b>Provider orders</b><small>sandbox</small></div><strong>'+count(prov.count)+'</strong></div>';
  }

  // Settings
  setText("settSnapshotName", snapshot._fileName||"Loaded");
  setText("settSnapshotSchema", count(m.schema_version));

  // Security
  setText("snapshotGenerated", snapshot.exported_at || "Timestamp unavailable");
  setText("snapshotSchema", count(m.schema_version));
  setText("snapshotMode", "Authenticated, account-scoped, read-only");
  setText("snapshotFileName", snapshot._fileName || "Loaded from file input");
  setText("diagSchema", count(m.schema_version));
  setText("diagReadonly", "Snapshot data is read into this page only and is not uploaded.");
  const auditRows=$("auditRows");
  if(auditRows){
    // Snapshot doesn't contain raw audit, but we can show summary from metrics
    auditRows.innerHTML = '<div class="row"><div><b>Snapshot metrics</b><small>read_only, owner-scoped</small></div><strong>'+count(m.schema_version)+'</strong></div><div class="muted" style="margin-top:8px">Full audit is in state DB state_audit table — not exported to snapshot for privacy.</div>';
  }

  // Status + toast
  const live = source === "live";
  $("snapshotStatus").textContent = live ? "Connected to local backend — live account data." : "Snapshot loaded — "+count(m.schema_version)+" schema, read-only.";
  $("snapshotStatus").className = "status success";
  document.querySelector(".status").textContent = live ? "LIVE LOCAL BACKEND" : "READ-ONLY SNAPSHOT";
  const foot = $("dashboardFootnote"); if(foot) foot.textContent = live ? "Live metrics from your local account database. Refreshes automatically while this page is open." : "Snapshot data only; no live connection. Loaded at "+(snapshot.exported_at||"unknown")+".";
  const mode = $("overviewMode"); if(mode) mode.textContent = live ? "Live local backend · account-scoped" : "Read-only snapshot";
  showToast(live ? "Connected to local backend" : "Snapshot loaded — "+count(m.schema_version)+" schema", "success");
}

function renderEmpty(){
  currentSnapshot=null; currentMetrics=null;
  ["dashTotalOpps","dashActiveProj","dashVerified","dashNet","dashOutstanding","dashActiveCust","dashTasks","dashMrr","dashRealized","dashUnverified","dashProjected","metricPipeline","metricInvoices","metricObservations","oppCount","oppActiveCount","oppAmount","svcCount","svcAmount","prodCount","prodAmount","custCount","custActive","projActive","projTotal","taskQueued","taskProjCount","taskCompletion","aiCount","ordersCount","ordersAmount","orderDrafts","invoiceCount","invoiceAmount","invoiceOutstanding","providerCount","providerEventsCount","payVerified","expCount","expAmount","expFees","ledgerNet","analyticsFunnel","analyticsMrr","queueCount","workflowCount","revTotalOpps","revActiveOpps","revAppsSubmitted","revPropsAccepted","revActiveCust","revActiveProj","revOrders","revInvoices","revOutstanding","revVerified","revExpenses","revFees","revNet","revAvgProject","revWorkload","revConversion","incomeRealized","incomeUnverified","incomeProjected"].forEach(id=>{
    const el=$(id); if(el) el.textContent="—";
  });
  ["metricIncome"].forEach(id=>{ const el=$(id); if(el) el.textContent="Not available"; });
  ["dashTotalOppsNote","dashVerifiedNote","dashNetNote","dashOutstandingNote","dashTasksNote","dashMrrNote","metricPipelineNote","metricInvoicesNote","metricObservationsNote","pipelineStatus","ledger11Status","providerStatus","incomeStatus","oppStatus","svcStatus","prodStatus","custStatus","propStatus","ordersStatus","invoiceStatus","providerCountNote","aiStatus","emergencyStatus","emergencyState","funnelStatus","mrrStatus","queueStatus","overviewMode","revTotalOppsNote","revOrdersNote","revInvoicesNote","revOutstandingNote","revVerifiedNote","revExpensesNote","revNetNote","revWorkloadNote","revConversionNote"].forEach(id=>{
    const el=$(id); if(el) el.textContent="No snapshot loaded";
  });
  ["oppByStatus","svcByStatus","prodByStatus","custByStatus","taskByStatus","ordersByStatus","invoiceByStatus","providerByStatus","providerByProvider","aiByStatus","analyticsBuckets","ledger11Rows","ledgerByKind","propByStatus","customerActivityDetail","projByCustomer","oppFunnelDetail","revenueChart","expenseChart","taskChart","oppChart","expChart","integrationStatus","auditRows"].forEach(id=>{
    const el=$(id); if(el) el.innerHTML='<div class="empty">No snapshot loaded — data appears after you create records and export a snapshot.</div>';
  });
  ["revenueChart","expenseChart","oppFunnelDetail","customerActivityDetail","incomeSeparation"].forEach(id=>{
    const el=$(id); if(el) el.innerHTML='<div class="empty">No snapshot loaded — create data to see charts.</div>';
  });
  const snapGen=$("snapshotGenerated"); if(snapGen) snapGen.textContent="Not loaded";
  const snapSch=$("snapshotSchema"); if(snapSch) snapSch.textContent="Not loaded";
  const snapMode=$("snapshotMode"); if(snapMode) snapMode.textContent="Waiting for a snapshot";
  const snapFile=$("snapshotFileName"); if(snapFile) snapFile.textContent="None loaded";
  const diagSch=$("diagSchema"); if(diagSch) diagSch.textContent="—";
  const settName=$("settSnapshotName"); if(settName) settName.textContent="None loaded";
  const settSchema=$("settSnapshotSchema"); if(settSchema) settSchema.textContent="—";
  $("snapshotStatus").textContent="No snapshot loaded. Your browser has not received any account data.";
  $("snapshotStatus").className="status";
  const foot=$("dashboardFootnote"); if(foot) foot.textContent="No account data loaded · snapshot data is read into this page only and is not uploaded.";
  const em=$("overviewEmpty"); if(em){ em.style.display="block"; em.textContent="No snapshot loaded — metrics show honest empty states."; }
  const overviewNote=$("overviewMode"); if(overviewNote) overviewNote.textContent="Waiting for a snapshot. No fabricated data is shown.";
}

function handleFile(file){
  const statusEl=$("snapshotStatus");
  if (!file) return;
  showLoading(true);
  statusEl.textContent="Loading snapshot…";
  statusEl.className="status";
  if (file.size > 1048576) { showLoading(false); statusEl.textContent="Snapshot exceeds 1 MB — refusing to load."; statusEl.className="status error"; showToast("Snapshot too large (>1 MB)", "error"); setTimeout(()=>statusEl.className="status",3000); return; }
  const reader = new FileReader();
  reader.onerror = ()=>{ showLoading(false); statusEl.textContent="Could not read file."; statusEl.className="status error"; showToast("Could not read file", "error"); };
  reader.onload = ()=>{
    try{
      const text = reader.result;
      const data = JSON.parse(text);
      data._fileName = file.name;
      showLoading(false);
      render(data);
      currentSnapshot=data;
      statusEl.className="status success";
    } catch(e){
      showLoading(false);
      statusEl.textContent = e && e.message ? e.message : "Could not load snapshot.";
      statusEl.className="status error";
      showToast(e.message||"Could not load snapshot", "error");
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
    showToast("Snapshot cleared", "success");
    if (inp) inp.value="";
    history.replaceState(null,"","#/dashboard");
    updateRoute();
  });
}
function initFilters(){
  ["oppSearch","svcSearch","custSearch"].forEach(id=>{
    const el=$(id);
    if(!el) return;
    el.addEventListener("input", ()=>{
      const q = el.value.toLowerCase().trim();
      // simple client-side filter for rows in its view
      const view = el.closest(".view");
      if(!view) return;
      view.querySelectorAll(".rows .row").forEach(row=>{
        const text = row.textContent.toLowerCase();
        row.style.display = text.includes(q) ? "" : "none";
      });
      if(!q) view.querySelectorAll(".rows .row").forEach(r=>r.style.display="");
    });
  });
  const clearOpp=$("oppClear");
  if(clearOpp) clearOpp.addEventListener("click", ()=>{
    const el=$("oppSearch"); if(el){ el.value=""; el.dispatchEvent(new Event("input")); }
  });
}
function initRevenueHandlers(){
  const btn=$("revFilterBtn"), clear=$("revClearFilter"), exp=$("revExportBtn"), expJ=$("revExportJsonBtn");
  if(btn) btn.addEventListener("click", ()=>{
    if(currentSnapshot) { render(currentSnapshot); showToast("Filter applied", "success"); }
    else { const el=$("revFilterStatus"); if(el) el.textContent="No snapshot — load a snapshot first."; showToast("No snapshot", "error"); }
  });
  if(clear) clear.addEventListener("click", ()=>{
    const f=$("revFrom"), t=$("revTo");
    if(f) f.value=""; if(t) t.value="";
    if(currentSnapshot) render(currentSnapshot);
    const el=$("revFilterStatus"); if(el) el.textContent="Showing all-time (no filter).";
    showToast("Filter cleared", "success");
  });
  function download(filename, text, mime){
    const blob=new Blob([text], {type: mime});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url; a.download=filename; a.click();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }
  if(exp) exp.addEventListener("click", ()=>{
    if(!currentMetrics || !currentMetrics.revenue_analytics){ showToast("No revenue data — load a snapshot first.", "error"); return; }
    try{
      const rev=currentMetrics.revenue_analytics;
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
      if(csv.length>1048576) { showToast("Export too large", "error"); return; }
      download("revenue-report.csv", csv, "text/csv");
      showToast("CSV downloaded", "success");
    } catch(e){ showToast("Export failed: "+(e.message||e), "error"); }
  });
  if(expJ) expJ.addEventListener("click", ()=>{
    if(!currentMetrics || !currentMetrics.revenue_analytics){ showToast("No revenue data", "error"); return; }
    const text=JSON.stringify(currentMetrics.revenue_analytics, null, 2);
    if(text.length>1048576) { showToast("Export too large", "error"); return; }
    const blob=new Blob([text], {type:"application/json"});
    const url=URL.createObjectURL(blob);
    const a=document.createElement("a");
    a.href=url; a.download="revenue-report.json"; a.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
    showToast("JSON downloaded", "success");
  });
}
const apiState = { csrf: "", account: null, timer: null, live: false };
async function apiJson(path, options){
  const response = await fetch(path, Object.assign({ credentials:"same-origin", cache:"no-store" }, options||{}));
  let body;
  try { body = await response.json(); } catch(_) { throw new Error("The local backend returned an invalid response."); }
  if(!response.ok || !body.ok) {
    const error=new Error(body.error && body.error.message ? body.error.message : "Local backend request failed.");
    error.status=response.status;
    throw error;
  }
  return body.data;
}

const LIVE_RESOURCES = {
  projects:{title:"Client projects",fields:[["customer_id","Customer ID","text",true],["title","Project title","text",true],["description","Project scope / description","textarea",false],["priority","Priority (0–9)","number",false],["deadline_at","Deadline (Unix seconds; optional)","number",false]],actions:{draft:["submit_for_review"],pending_review:["approve","reject"],approved:["start","cancel"],scheduled:["start","cancel"],in_progress:["complete","cancel"]}},

  opportunities:{title:"Opportunity records",fields:[["title","Title","text",true],["source_url","Source URL","url",true],["budget_paise","Budget (paise)","number",true],["deadline_at","Deadline (Unix seconds; optional)","number",false],["risk_level","Risk: low / medium / high / unknown","text",false],["skill_tags","Skills / tags","text",false]],actions:{discovered:["vetted"],vetted:["pursuing"]}},
  services:{title:"Service catalog",fields:[["title","Service title","text",true],["scope","Scope","textarea",true],["deliverable","Deliverable","text",true],["price_paise","Price (paise)","number",true],["revisions","Revisions","number",true]],actions:{draft:["publish","archive"],published:["archive"]}},
  proposals:{title:"Proposals",fields:[["service_id","Service ID","text",true],["customer_id","Customer ID (optional)","text",false],["title","Proposal title","text",true],["description","Description","textarea",true],["price_paise","Price (paise)","number",true],["delivery_days","Delivery days","number",true]],actions:{draft:["send"],sent:["accept","reject","cancel"],rejected:["archive"],accepted:["archive"]}},
  orders:{title:"Customer orders",fields:[["proposal_id","Accepted customer-linked proposal ID","text",true]],actions:{draft:["cancel"]}},
  customers:{title:"Customer CRM",fields:[["name","Customer name","text",true],["contact","Email / contact","text",true],["source","Lead source","text",false]],actions:{lead:["contacted","rejected"],contacted:["converted","rejected"]}},
  tasks:{title:"Project tasks",fields:[["title","Task title","text",true],["description","Description","textarea",false],["priority","Priority (1–10)","number",false],["deadline_at","Deadline (Unix seconds; optional)","number",false]],actions:{draft:["submit_for_review"],pending_review:["approve","reject"],approved:["start","cancel"],scheduled:["start","cancel"],in_progress:["complete","cancel"]}},
  products:{title:"Digital products",fields:[["title","Product title","text",true],["description","Description","textarea",true],["version","Version","text",true],["price_paise","Price (paise)","number",true]],actions:{draft:["publish","archive"],published:["archive"]}},
  invoices:{title:"Invoices",fields:[["order_id","Order draft ID","text",true],["due_at","Due date (Unix seconds; optional)","number",false],["billing_name","Billing name","text",false],["billing_contact","Billing contact","text",false],["billing_address","Billing address","textarea",false]],actions:{draft:["issue"]}},
  payments:{title:"Payment records",fields:[["amount_paise","Amount (paise)","number",true],["provider","Provider (manual / sandbox)","text",false],["provider_ref","Provider reference","text",false],["invoice_id","Invoice ID (optional)","text",false],["order_id","Order ID (optional)","text",false]],actions:{initiated:["received","failed"],received:["settled","failed"],settled:["refunded"]}},
  expenses:{title:"Expenses",fields:[["amount_paise","Amount (paise)","number",true],["category","Category","text",true],["note","Note","textarea",false]]},
  ledger:{title:"Ledger entries (unverified)",fields:[["kind","Kind: income / expense / refund / fee","text",true],["amount_paise","Amount (paise)","number",true],["note","Note","text",false]]}
};
function liveFieldHtml(field){
  const [name,label,type,required]=field;
  const tag=type==="textarea"?"textarea":"input";
  const inputType=type==="number"?"number":type==="url"?"url":"text";
  return '<label class="live-field">'+escapeHtml(label)+(required?' <span aria-hidden="true">*</span>':'')+
    (tag==="textarea"?'<textarea name="'+name+'" rows="2" '+(required?'required':'')+'></textarea>':'<input name="'+name+'" type="'+inputType+'" '+(type==="number"?'min="0" step="1"':'')+(required?' required':'')+'>')+'</label>';
}
function liveItemSummary(item){
  const preferred=["title","name","status","contact","source_url","description","amount_paise","budget_paise","price_paise","category","kind"];
  return preferred.filter(k=>item[k]!==undefined && item[k]!==null && item[k]!=="").slice(0,5).map(k=>{
    const v=(k.endsWith("_paise") && Number.isSafeInteger(item[k]))?rupees(item[k]):String(item[k]);
    return '<span><b>'+escapeHtml(k.replaceAll("_"," "))+'</b> '+escapeHtml(v.slice(0,180))+'</span>';
  }).join("");
}
function liveActionButtons(resource,item){
  const actions=LIVE_RESOURCES[resource].actions||{};
  const names=actions[item.status]||[];
  return names.map(action=>'<button type="button" class="button ghost" data-live-action="'+escapeHtml(action)+'" data-live-id="'+escapeHtml(item.task_id||item.id)+'" data-live-resource="'+resource+'">'+escapeHtml(action.replaceAll("_"," "))+'</button>').join("");
}
async function loadLiveResource(resource){
  const root=document.querySelector('[data-live-resource="'+resource+'"]');
  if(!root || !apiState.account) return;
  try{
    const result=await apiJson('/api/v1/workspace/'+resource+'?limit=50');
    const rows=result.items||[];
    const list=rows.length?rows.map(item=>'<article class="live-record"><div class="live-record-main"><strong>'+escapeHtml(item.title||item.name||item.invoice_number||item.id)+'</strong><small>'+escapeHtml(item.status||"record")+' · '+escapeHtml(item.id)+'</small><div class="live-record-details">'+liveItemSummary(item)+'</div></div><div class="live-record-actions">'+liveActionButtons(resource,item)+(resource==="services"?'<button type="button" class="button ghost" data-live-edit="'+escapeHtml(item.id)+'" data-live-title="'+escapeHtml(item.title||"")+'">Edit title</button>':'')+(resource==="customers"?'<button type="button" class="button ghost" data-live-customer-details="'+escapeHtml(item.id)+'">Notes &amp; requirements</button>':'')+'</div></article>').join(""):'<div class="empty">No records yet. Create the first record above.</div>';
    root.querySelector(".live-record-list").innerHTML=list;
    root.querySelector(".live-resource-status").textContent=rows.length+' record(s) loaded from the local database.';
  }catch(err){
    root.querySelector(".live-resource-status").textContent=err.status===401?"Sign in to load account records.":"Backend unavailable; offline snapshot remains available.";
    root.querySelector(".live-record-list").innerHTML='<div class="empty">Live records unavailable. '+escapeHtml(err.message)+'</div>';
  }
}
async function openCustomerDetails(root, customerId){
  const panel=root.querySelector(".customer-detail-workspace");
  if(!panel) return;
  panel.hidden=false;
  panel.innerHTML='<div class="panel-head"><h3>Customer notes &amp; requirements</h3><span class="muted">Loading…</span></div>';
  try{
    const base="/api/v1/workspace/customers/"+encodeURIComponent(customerId);
    const [notes,requirements]=await Promise.all([
      apiJson(base+"/notes"),
      apiJson(base+"/requirements")
    ]);
    const renderRows=(items,key)=>items.length?items.map(item=>'<li>'+escapeHtml(item[key]||"")+'</li>').join(""):'<li class="muted">None recorded yet.</li>';
    panel.innerHTML='<div class="panel-head"><h3>Customer notes &amp; requirements</h3><button type="button" class="button ghost" data-customer-details-close>Close</button></div>'+
      '<p class="muted">Customer record: '+escapeHtml(customerId)+'</p>'+
      '<div class="customer-detail-columns"><section><h4>Notes</h4><ul class="customer-detail-list">'+renderRows(notes.items||[],"note")+'</ul>'+
      '<form class="customer-detail-form" data-customer-detail-kind="notes"><label class="live-field">Add a note<textarea name="note" rows="2" maxlength="2000" required></textarea></label><button class="button" type="submit">Save note</button></form></section>'+
      '<section><h4>Requirements</h4><ul class="customer-detail-list">'+renderRows(requirements.items||[],"requirement")+'</ul>'+
      '<form class="customer-detail-form" data-customer-detail-kind="requirements"><label class="live-field">Add a requirement<input name="requirement" type="text" maxlength="500" required></label><button class="button" type="submit">Save requirement</button></form></section></div>';
    panel.dataset.customerId=customerId;
  }catch(err){
    panel.innerHTML='<div class="panel-head"><h3>Customer details unavailable</h3><button type="button" class="button ghost" data-customer-details-close>Close</button></div><p class="muted" role="status">'+escapeHtml(err.message)+'</p>';
  }
}
function clearLiveResources(){
  document.querySelectorAll("[data-live-resource]").forEach(root=>{
    const status=root.querySelector(".live-resource-status"), list=root.querySelector(".live-record-list");
    if(status) status.textContent="Sign in to load live records.";
    if(list) list.innerHTML='<div class="empty">Sign in to load records from your local database.</div>';
  });
}
async function refreshLiveResources(){
  if(!apiState.account) return;
  await Promise.all(Object.keys(LIVE_RESOURCES).map(loadLiveResource));
}
function liveIdempotencyKey(){ return window.crypto && typeof window.crypto.randomUUID==="function" ? window.crypto.randomUUID() : String(Date.now())+"-"+Math.random().toString(36).slice(2); }
function initWorkspaceCrud(){
  document.querySelectorAll("[data-live-resource]").forEach(root=>{
    const resource=root.getAttribute("data-live-resource"), config=LIVE_RESOURCES[resource];
    if(!config) return;
    root.innerHTML='<div class="panel-head"><h2>'+escapeHtml(config.title)+'</h2><span class="muted live-resource-status">Sign in to load live records.</span></div>'+
      '<form class="live-create-form" data-live-create="'+resource+'"><div class="live-form-grid">'+config.fields.map(liveFieldHtml).join("")+'</div><button class="button" type="submit">Create record</button></form>'+
      '<h3>Account records</h3><div class="live-record-list"><div class="empty">Sign in to load records from your local database.</div></div>'+(resource==="customers"?'<section class="customer-detail-workspace" aria-live="polite" hidden></section>':'');
    root.addEventListener("submit",async e=>{
      const detailForm=e.target.closest("[data-customer-detail-kind]");
      if(detailForm){
        e.preventDefault();
        if(!apiState.account){showToast("Sign in to modify customer details.","error");return;}
        const customerRoot=root;
        const customerId=customerRoot.querySelector(".customer-detail-workspace").dataset.customerId;
        const field=detailForm.dataset.customerDetailKind==="notes"?"note":"requirement";
        const value=detailForm.elements.namedItem(field).value.trim();
        if(!value) return;
        try{
          await apiJson("/api/v1/workspace/customers/"+encodeURIComponent(customerId)+"/"+detailForm.dataset.customerDetailKind,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf,"Idempotency-Key":liveIdempotencyKey()},body:JSON.stringify({[field]:value})});
          showToast(field==="note"?"Note saved.":"Requirement saved.","success");
          await openCustomerDetails(customerRoot,customerId);
        }catch(err){showToast(err.message,"error");}
        return;
      }
      const form=e.target.closest("[data-live-create]");
      if(!form) return;
      e.preventDefault();
      if(!apiState.account){showToast("Sign in to create records.","error");return;}
      const data={};
      for(const field of config.fields){
        const [name,,type]=field, el=form.elements.namedItem(name);
        if(!el || el.value==="") continue;
        data[name]=type==="number"?Number(el.value):el.value.trim();
      }
      try{
        await apiJson("/api/v1/workspace/"+resource,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf,"Idempotency-Key":liveIdempotencyKey()},body:JSON.stringify(data)});
        form.reset(); showToast("Record created in your local account.","success");
        await loadLiveResource(resource); await refreshLiveDashboard(false);
      }catch(err){showToast(err.message,"error");}
    });
    root.addEventListener("click",async e=>{
      const detail=e.target.closest("[data-live-customer-details]");
      const close=e.target.closest("[data-customer-details-close]");
      if(detail){
        if(!apiState.account){showToast("Sign in to view customer details.","error");return;}
        await openCustomerDetails(root,detail.dataset.liveCustomerDetails);
        return;
      }
      if(close){const panel=root.querySelector(".customer-detail-workspace");if(panel){panel.hidden=true;panel.innerHTML="";}return;}
      const action=e.target.closest("[data-live-action]");
      const edit=e.target.closest("[data-live-edit]");
      if(!action && !edit) return;
      if(!apiState.account){showToast("Sign in to modify records.","error");return;}
      const id=action?action.dataset.liveId:edit.dataset.liveEdit;
      const verb=action?action.dataset.liveAction:"edit";
      try{
        if(verb==="edit"){
          const title=window.prompt("Update service title",edit.dataset.liveTitle||"");
          if(title===null) return;
          await apiJson("/api/v1/workspace/services/"+encodeURIComponent(id),{method:"PATCH",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf,"Idempotency-Key":liveIdempotencyKey()},body:JSON.stringify({title})});
        }else{
          await apiJson("/api/v1/workspace/"+resource+"/"+encodeURIComponent(id)+"/actions/"+encodeURIComponent(verb),{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf,"Idempotency-Key":crypto.randomUUID()},body:"{}"});
        }
        showToast("Record updated.","success"); await loadLiveResource(resource); await refreshLiveDashboard(false);
      }catch(err){showToast(err.message,"error");}
    });
  });
}

async 
async function aiPost(path,payload){
  return apiJson(path,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf,"Idempotency-Key":liveIdempotencyKey()},body:JSON.stringify(payload||{})});
}
function aiPrint(id,value){const el=$(id);if(el)el.textContent=typeof value==="string"?value:JSON.stringify(value,null,2);}
async function refreshAiWorkspace(showErrors){
  if(!apiState.account)return;
  try{
    const [status,queue]=await Promise.all([apiJson("/api/v1/ai/status"),apiJson("/api/v1/ai/approvals?limit=50")]);
    const p=status.provider||{};
    setText("aiProviderInfo",(p.provider||"unknown")+" · "+(p.mode||"")+" · "+(p.model||""));
    setText("aiProviderStatus",status.provider_available?"Available":"Unavailable");
    const pending=(queue.items||[]).filter(x=>x.effective_status==="pending").length;
    setText("aiCount",pending+" pending / "+(queue.count||0)+" total");
    const stopped=!!status.emergency_stopped;
    setText("emergencyState",stopped?"STOPPED":"Clear");
    setText("emergencyStatus",stopped?"Consequential actions blocked":"No emergency stop active");
    const select=$("aiToolName");
    if(select){const old=select.value;select.innerHTML='<option value="">Choose a tool</option>'+(status.tools||[]).map(t=>'<option value="'+escapeHtml(t.name)+'">'+escapeHtml(t.name)+' · '+escapeHtml(t.scope)+'</option>').join("");if(old)select.value=old;}
    const list=$("aiApprovalList");
    if(list){const items=queue.items||[];list.innerHTML=items.length?items.map(item=>{
      let args={};try{args=JSON.parse(item.args_json||"{}");}catch(_){}
      const state=item.effective_status||item.status||"unknown";
      const buttons=state==="pending"?'<button class="button ghost" type="button" data-ai-decision="approved" data-ai-id="'+escapeHtml(item.id)+'">Approve</button><button class="button ghost" type="button" data-ai-decision="rejected" data-ai-id="'+escapeHtml(item.id)+'">Reject</button>':(state==="approved"?'<button class="button ghost" type="button" data-ai-revoke="1" data-ai-id="'+escapeHtml(item.id)+'">Revoke approval</button>':"");
      return '<article class="live-record"><div class="live-record-main"><strong>'+escapeHtml(item.tool_name||"Approval")+'</strong><small>'+escapeHtml(state)+' · expires '+escapeHtml(String(item.expires_at||""))+'</small><div class="live-record-details"><span>'+escapeHtml(JSON.stringify(args))+'</span></div></div><div class="live-record-actions">'+buttons+'</div></article>';
    }).join(""):'<div class="empty">No approval records yet.</div>';}
  }catch(err){if(showErrors!==false)showToast(err.message,"error");}
}
function clearAiWorkspace(){
  setText("aiProviderInfo","Sign in to inspect local provider");
  setText("aiProviderStatus","—");setText("aiCount","—");
  setText("emergencyState","—");setText("emergencyStatus","Safety control");
  const list=$("aiApprovalList");if(list)list.innerHTML='<div class="empty">Sign in to load your approvals.</div>';
  const tools=$("aiToolName");if(tools)tools.innerHTML='<option value="">Load tools after sign-in</option>';
  aiPrint("aiChatOutput","No AI response yet.");aiPrint("aiProposalOutput","No proposal prepared.");
}
function initAiWorkspace(){
  const chat=$("aiChatForm"),proposal=$("aiProposalForm"),list=$("aiApprovalList");
  if(chat)chat.addEventListener("submit",async e=>{
    e.preventDefault();if(!apiState.account){showToast("Sign in to use the AI assistant.","error");return;}
    aiPrint("aiChatOutput","Generating draft…");
    try{const payload={prompt:$("aiPrompt").value.trim()};if($("aiExternalContent").value.trim())payload.external_content=$("aiExternalContent").value;
      const result=await aiPost("/api/v1/ai/chat",payload);aiPrint("aiChatOutput",result.response);
    }catch(err){aiPrint("aiChatOutput",err.message);showToast(err.message,"error");}
  });
  if(proposal)proposal.addEventListener("submit",async e=>{
    e.preventDefault();if(!apiState.account){showToast("Sign in to prepare a tool preview.","error");return;}
    let args;try{args=JSON.parse($("aiToolArgs").value);}catch(_){showToast("Arguments must be valid JSON.","error");return;}
    if(!args||typeof args!=="object"||Array.isArray(args)){showToast("Arguments must be a JSON object.","error");return;}
    try{const result=await aiPost("/api/v1/ai/proposals",{tool_name:$("aiToolName").value,args});aiPrint("aiProposalOutput",result.proposal);await refreshAiWorkspace(false);}
    catch(err){aiPrint("aiProposalOutput",err.message);showToast(err.message,"error");}
  });
  if(list)list.addEventListener("click",async e=>{
    const decision=e.target.closest("[data-ai-decision]"),revoke=e.target.closest("[data-ai-revoke]");if(!decision&&!revoke)return;
    const id=(decision||revoke).dataset.aiId;
    if(decision){const action=decision.dataset.aiDecision;if(!window.confirm((action==="approved"?"Approve":"Reject")+" this consequential request? This records your decision but does not execute the action."))return;
      try{await aiPost("/api/v1/ai/approvals/"+encodeURIComponent(id)+"/decision",{decision:action,confirmed:true});showToast("Approval decision recorded.","success");}catch(err){showToast(err.message,"error");}
    }else{if(!window.confirm("Revoke this approval?"))return;try{await aiPost("/api/v1/ai/approvals/"+encodeURIComponent(id)+"/revoke",{});showToast("Approval revoked.","success");}catch(err){showToast(err.message,"error");}}
    await refreshAiWorkspace(false);
  });
  const refresh=$("aiRefreshButton");if(refresh)refresh.addEventListener("click",()=>refreshAiWorkspace(true));
  const stop=$("aiEmergencyStop");if(stop)stop.addEventListener("click",async()=>{
    if(!apiState.account){showToast("Sign in first.","error");return;}if(!window.confirm("Activate emergency stop?"))return;
    try{await aiPost("/api/v1/ai/emergency/stop",{confirmed:true,reason:"Activated from Earn OS dashboard"});showToast("Emergency stop activated.","success");await refreshAiWorkspace(false);}catch(err){showToast(err.message,"error");}
  });
  const clear=$("aiEmergencyClear");if(clear)clear.addEventListener("click",async()=>{
    if(!apiState.account){showToast("Sign in first.","error");return;}if(!window.confirm("Clear emergency stop?"))return;
    try{await aiPost("/api/v1/ai/emergency/clear",{confirmed:true});showToast("Emergency stop cleared.","success");await refreshAiWorkspace(false);}catch(err){showToast(err.message,"error");}
  });
}

function initLocalApi(){
  const form=$("loginForm"), logout=$("logoutButton"), user=$("signedInAs");
  try{
    const csrf=await apiJson("/api/v1/csrf"); apiState.csrf=csrf.csrf_token;
    const me=await apiJson("/api/v1/me"); apiState.account=me.account;
    if(form) form.hidden=true; if(logout) logout.hidden=false;
    if(user) user.textContent="Signed in: "+(me.account.display_name||me.account.username);
    await refreshLiveDashboard();
    await refreshLiveResources();
    await refreshAiWorkspace(false);
    apiState.timer=window.setInterval(()=>{ if(document.visibilityState==="visible" && apiState.account){ refreshLiveDashboard(false); refreshAiWorkspace(false); } },30000);
  }catch(_){
    if(form) form.hidden=false; if(logout) logout.hidden=true; if(user) user.textContent="";
    const status=$("snapshotStatus");
    if(status && !currentSnapshot){ status.textContent="Local backend available when started — sign in, or load an offline snapshot."; status.className="status"; }
  }
  if(form) form.addEventListener("submit", async e=>{
    e.preventDefault();
    const username=$("loginUsername"), password=$("loginPassword"), message=$("loginMessage");
    if(!apiState.csrf){ try{ const c=await apiJson("/api/v1/csrf"); apiState.csrf=c.csrf_token; }catch(_){ if(message) message.textContent="Cannot reach local backend. Start Earn OS with python -m ai_earn serve."; return; } }
    try{
      const data=await apiJson("/api/v1/auth/login",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf},body:JSON.stringify({username:username.value.trim(),password:password.value})});
      apiState.account=data.account; password.value=""; form.hidden=true; if(logout) logout.hidden=false;
      if(user) user.textContent="Signed in: "+(data.account.display_name||data.account.username);
      await refreshLiveDashboard();
      await refreshLiveResources();
      await refreshAiWorkspace(false);
      if(apiState.timer) clearInterval(apiState.timer);
      apiState.timer=window.setInterval(()=>{ if(document.visibilityState==="visible" && apiState.account){ refreshLiveDashboard(false); refreshAiWorkspace(false); } },30000);
    }catch(err){ if(message) message.textContent=err.message; }
  });
  if(logout) logout.addEventListener("click",async()=>{
    try{ await apiJson("/api/v1/auth/logout",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":apiState.csrf},body:"{}"}); }
    catch(_){ showToast("Could not confirm sign out. The local session may still be active.","error"); return; }
    apiState.account=null; apiState.live=false; if(apiState.timer) clearInterval(apiState.timer); apiState.timer=null; clearLiveResources(); clearAiWorkspace();
    if(form) form.hidden=false; logout.hidden=true; if(user) user.textContent=""; renderEmpty();
    const status=$("snapshotStatus"); if(status) status.textContent="Signed out. Load an offline snapshot or sign in again.";
  });
}
async function refreshLiveDashboard(showErrors){
  try{
    const data=await apiJson("/api/v1/dashboard/metrics");
    render(data,"live"); currentSnapshot=data; apiState.live=true;
    const updated=$("lastUpdated"); if(updated) updated.textContent="Last refreshed: "+new Date().toLocaleTimeString();
  }catch(err){
    apiState.live=false;
    if(err.status===401){
      apiState.account=null;
      if(apiState.timer) clearInterval(apiState.timer); apiState.timer=null; clearLiveResources(); clearAiWorkspace();
      const form=$("loginForm"), logout=$("logoutButton"), user=$("signedInAs");
      if(form) form.hidden=false; if(logout) logout.hidden=true; if(user) user.textContent="";
      const status=$("snapshotStatus"); if(status){ status.textContent="Your local session expired. Sign in again to refresh live data."; status.className="status error"; }
    }else{
      if(showErrors !== false) showToast(err.message,"error");
      const status=$("snapshotStatus"); if(status){ status.textContent="Live backend disconnected. Existing offline snapshot remains available."; status.className="status error"; }
    }
  }
}
function initSettingsForm(){
  const form=$("settingsForm");
  if(!form) return;
  const nameEl=$("settName"), emailEl=$("settEmail");
  const nameErr=$("settNameErr"), emailErr=$("settEmailErr");
  // Load saved
  try{
    const saved=JSON.parse(localStorage.getItem("earn_os_settings")||"{}");
    if(saved.name && nameEl) nameEl.value=saved.name;
    if(saved.email && emailEl) emailEl.value=saved.email;
    if(saved.currency && $("settCurrency")) $("settCurrency").value=saved.currency;
    if(saved.notify && $("settNotify")) $("settNotify").value=saved.notify;
  }catch(_){}
  function validate(){
    let ok=true;
    const name = nameEl ? nameEl.value.trim() : "";
    const email = emailEl ? emailEl.value.trim() : "";
    if(nameEl){
      if(!name || name.length<2){
        nameEl.classList.add("invalid"); nameErr.textContent="Name required (min 2)."; ok=false;
      } else if(name.length>40){
        nameEl.classList.add("invalid"); nameErr.textContent="Max 40 characters."; ok=false;
      } else { nameEl.classList.remove("invalid"); nameErr.textContent=""; }
    }
    if(emailEl){
      const re=/^[^\s@]+@[^\s@]+\.[^\s@]+$/;
      if(!email){ emailEl.classList.add("invalid"); emailErr.textContent="Email required."; ok=false; }
      else if(!re.test(email)){ emailEl.classList.add("invalid"); emailErr.textContent="Enter a valid email."; ok=false; }
      else { emailEl.classList.remove("invalid"); emailErr.textContent=""; }
    }
    return ok;
  }
  if(nameEl) nameEl.addEventListener("input", validate);
  if(emailEl) emailEl.addEventListener("input", validate);
  form.addEventListener("submit", (e)=>{
    e.preventDefault();
    if(!validate()){ showToast("Fix validation errors", "error"); return; }
    const data={ name:nameEl.value.trim(), email:emailEl.value.trim(), currency:$("settCurrency").value, notify:$("settNotify").value };
    try{ localStorage.setItem("earn_os_settings", JSON.stringify(data)); }catch(_){}
    showToast("Settings saved", "success");
  });
  const resetBtn=$("settReset");
  if(resetBtn) resetBtn.addEventListener("click", ()=>{
    if(nameEl) nameEl.value=""; if(emailEl) emailEl.value=""; if($("settCurrency")) $("settCurrency").value="INR"; if($("settNotify")) $("settNotify").value="all";
    if(nameErr) nameErr.textContent=""; if(emailErr) emailErr.textContent="";
    if(nameEl) nameEl.classList.remove("invalid"); if(emailEl) emailEl.classList.remove("invalid");
    try{ localStorage.removeItem("earn_os_settings"); }catch(_){}
    showToast("Settings reset", "success");
  });
}

document.addEventListener("DOMContentLoaded", ()=>{
  initTheme();
  initNavToggle();
  initRouter();
  initSnapshotLoader();
  initFilters();
  initRevenueHandlers();
  initSettingsForm();
  initWorkspaceCrud();
  initAiWorkspace();
  renderEmpty();
  initLocalApi();
});
