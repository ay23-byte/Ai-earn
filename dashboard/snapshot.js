// Earn OS local snapshot renderer. No network requests or credential handling.
"use strict";
const $ = (id) => document.getElementById(id);
const count = (v) => Number.isSafeInteger(v) && v >= 0 ? String(v) : "0";
const rupees = (v) => new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR"}).format((Number.isSafeInteger(v) && v >= 0 ? v : 0) / 100);
function render(s) {
  if (!s || s.format !== "ai-earn-dashboard-snapshot" || s.version !== 1 || !s.metrics || s.metrics.read_only !== true) throw Error("Unsupported snapshot.");
  const m=s.metrics, w=m.work||{}, c=m.commercial||{}, p=m.asserted_pipeline||{}, o=m.payment_observations||{}, i=m.income||{}, opp=m.opportunities||{}, svc=m.services||{}, pf=m.portfolio||{}, prod=m.products||{}, cust=m.customers||{}, comm=m.communications||{}, led=m.ledger||{}, foll=m.followups||{}, inv=m.invoices||{}, leg11=m.ledger_11||{}, prov=m.provider_orders||{}, provevt=m.provider_events||{}, aia=m.ai_approvals||{}, eme=m.ai_emergency||{}, ana=m.analytics||{};
  $("metricPipeline").textContent=rupees(p.amount_paise);
  $("metricPipelineNote").textContent=count(p.count)+" unverified assertion(s), not revenue";
  $("metricInvoices").textContent=rupees((c.invoice_drafts||{}).amount_paise);
  $("metricInvoicesNote").textContent=count((c.invoice_drafts||{}).count)+" invoice draft(s), not issued";
  $("metricObservations").textContent=rupees(o.amount_paise);
  $("metricObservationsNote").textContent=count(o.count)+" unverified observation(s)";
  $("metricIncome").textContent=i.status==="not_available_no_verified_payment_ledger"?"Not available":rupees(i.verified_amount_paise);
  $("metricIncomeNote").textContent=i.note||"No verified payment ledger";
  $("workOpportunities").textContent=count(w.opportunity_drafts);
  $("workOpps").textContent=count(opp.count);

  $("workPlans").textContent=count(w.task_plans);
  $("workQueued").textContent=count(w.queued_tasks);
  $("workWorkflows").textContent=count(w.task_workflows);
  $("workOrders").textContent=count((c.order_drafts||{}).count);
  $("workInvoices").textContent=count((c.invoice_drafts||{}).count);
  $("workServices").textContent=count(svc.count);
  $("workPortfolio").textContent=count(pf.count);
  $("workProducts").textContent=count(prod.count);
  $("workCustomers").textContent=count(cust.count);
  $("workComms").textContent=count(comm.count);
  $("workLedger").textContent=count(led.count);
  $("workLedgerNet").textContent=rupees(led.net_paise !== undefined ? led.net_paise : 0);
  $("workFollowups").textContent=count(foll.count);
  $("workInvoicesIssued").textContent=count(inv.count);
  $("workLedger11").textContent=(leg11.proposed ? count(leg11.proposed.count) + "/" + count(leg11.invoices_issued ? leg11.invoices_issued.count : 0) : "—");
  $("workProvider").textContent=count(prov.count);
  $("workProviderEvents").textContent=count(provevt.count);
  $("workAI").textContent=count(aia.count);
  $("workEmergency").textContent=eme.stopped ? "STOPPED" : (eme.stopped===false ? "clear" : "—");
  $("workFunnel").textContent=ana.funnel ? (count(ana.funnel.opportunities)+"/"+count(ana.funnel.customers)+"/"+count(ana.funnel.invoices)) : "—";
  $("workMRR").textContent=ana.mrr ? rupees(ana.mrr.total_paise||0) : "—";
  $("pipelineStatus").textContent=count(p.count)+" asserted record(s), not verified";
  $("paymentStatus").textContent=count(o.count)+" unverified observation(s)";
  $("sandboxStatus").textContent=count((m.sandbox_payments||{}).count)+" sandbox event(s) only";
  $("opportunityStatus").textContent=count(opp.count)+" discovered (unverified)";
  $("serviceStatus").textContent=count(svc.count)+" service draft(s), unverified";
  $("productStatus").textContent=count(prod.count)+" product draft(s), unverified";
  $("customerStatus").textContent=count(cust.count)+" lead(s), unverified";
  $("commStatus").textContent=count(comm.count)+" comm(s), unverified";
  $("ledgerStatus").textContent=count(led.count)+" ledger entries, net "+rupees(led.net_paise !== undefined ? led.net_paise : 0);
  $("followupStatus").textContent=count(foll.count)+" followup(s), unverified";
  $("invoiceStatus").textContent=count(inv.count)+" invoice(s), unverified";
  $("ledger11Status").textContent= (leg11.proposed ? count(leg11.proposed.count)+" proposed, "+count((leg11.invoices_issued||{}).count)+" issued, net "+rupees((leg11.ledger_net_unverified||{}).amount_paise||0) : "No 11-concept");
  $("providerStatus").textContent=count(prov.count)+" provider order(s), sandbox";
  $("aiStatus").textContent=count(aia.count)+" approval(s), gated";
  $("emergencyStatus").textContent=eme.stopped ? "STOPPED: " + (eme.reason||"emergency") : "clear: consequential allowed if approved";
  $("funnelStatus").textContent=ana.funnel ? count(ana.funnel.opportunities)+" opp, "+count(ana.funnel.customers)+" cust, "+count(ana.funnel.invoices)+" inv" : "No analytics";
  $("mrrStatus").textContent=ana.mrr ? rupees(ana.mrr.total_paise||0)+" total, "+count(ana.mrr.months_with_income)+" months" : "No MRR";

  $("incomeStatus").textContent=i.status==="not_available_no_verified_payment_ledger"?"No verified payment ledger":"Verification: "+(i.status||"unknown");
  $("snapshotGenerated").textContent=s.exported_at||"Timestamp unavailable";
  $("snapshotSchema").textContent=count(m.schema_version);
  $("snapshotMode").textContent="Authenticated, account-scoped, read-only";
  $("snapshotStatus").textContent="Snapshot loaded";
  $("dashboardFootnote").textContent="Snapshot data only; no live connection.";
  document.querySelector(".status").textContent="READ-ONLY SNAPSHOT";
}
$("snapshotFile").addEventListener("change", async (event) => {
  const file=event.target.files && event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 1048576) throw Error("Snapshot exceeds 1 MB.");
    render(JSON.parse(await file.text()));
  } catch (error) {
    $("snapshotStatus").textContent=error.message || "Could not load snapshot.";
    event.target.value="";
  }
});
$("clearSnapshot").addEventListener("click", () => {
  ["metricPipeline","metricInvoices","metricObservations","metricIncome","workOpportunities","workOpps","workPlans","workQueued","workWorkflows","workOrders","workInvoices","workServices","workPortfolio","workProducts","workCustomers","workComms","workLedger","workLedgerNet","workFollowups","workInvoicesIssued","workLedger11","workProvider","workProviderEvents","workAI","workEmergency","workFunnel","workMRR"].forEach(id => $(id).textContent="—");

  ["metricPipelineNote","metricInvoicesNote","metricObservationsNote","metricIncomeNote","pipelineStatus","paymentStatus","sandboxStatus","opportunityStatus","serviceStatus","productStatus","customerStatus","commStatus","ledgerStatus","followupStatus","invoiceStatus","ledger11Status","providerStatus","aiStatus","emergencyStatus","funnelStatus","mrrStatus","incomeStatus"].forEach(id => $(id).textContent="No snapshot loaded");
  ["snapshotGenerated","snapshotSchema"].forEach(id => $(id).textContent="Not loaded");
  $("snapshotMode").textContent="Waiting for a snapshot";
  $("snapshotStatus").textContent="No snapshot loaded";
  $("dashboardFootnote").textContent="No account data loaded.";
  document.querySelector(".status").textContent="NO DATA LOADED";
  $("snapshotFile").value="";
});
