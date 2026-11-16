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
  // AI
  setText("aiCount", count(aia.count));
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
  ["metricPipeline","metricInvoices","metricObservations","oppCount","oppAmount","svcCount","svcAmount","portfolioCount","prodCount","prodAmount","custCount","commCount","followCount","ledgerCount","ledgerNet","invoiceCount","invoiceAmount","providerCount","providerEventsCount","aiCount","workOpps","workServices","workProducts","workCustomers","workLedgerNet","workInvoicesIssued","workProvider","workAI","workFunnel","analyticsFunnel","analyticsMrr","queueCount","workflowCount"].forEach(id=>{
    const el=$(id); if(el) el.textContent="—";
  });
  ["metricIncome"].forEach(id=>{ const el=$(id); if(el) el.textContent="Not available"; });
  ["metricPipelineNote","metricInvoicesNote","metricObservationsNote","pipelineStatus","ledger11Status","providerStatus","incomeStatus","oppStatus","svcStatus","portfolioStatus","prodStatus","custStatus","commStatus","followStatus","ledgerStatus","invoiceStatus","providerCountNote","aiStatus","emergencyStatus","emergencyState","funnelStatus","mrrStatus","queueStatus","overviewMode"].forEach(id=>{
    const el=$(id); if(el) el.textContent="No snapshot loaded";
  });
  ["oppByStatus","svcByStatus","prodByStatus","custByStatus","commByStatus","followByStatus","ledgerByKind","invoiceByStatus","providerByStatus","providerByProvider","aiByStatus","analyticsBuckets","ledger11Rows"].forEach(id=>{
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

document.addEventListener("DOMContentLoaded", ()=>{
  initTheme();
  initNavToggle();
  initRouter();
  initSnapshotLoader();
  renderEmpty();
});
