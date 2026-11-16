// Earn OS local snapshot renderer. No network requests or credential handling.
"use strict";
const $ = (id) => document.getElementById(id);
const count = (v) => Number.isSafeInteger(v) && v >= 0 ? String(v) : "0";
const rupees = (v) => new Intl.NumberFormat("en-IN", {style:"currency",currency:"INR"}).format((Number.isSafeInteger(v) && v >= 0 ? v : 0) / 100);
function render(s) {
  if (!s || s.format !== "ai-earn-dashboard-snapshot" || s.version !== 1 || !s.metrics || s.metrics.read_only !== true) throw Error("Unsupported snapshot.");
  const m=s.metrics, w=m.work||{}, c=m.commercial||{}, p=m.asserted_pipeline||{}, o=m.payment_observations||{}, i=m.income||{}, opp=m.opportunities||{};
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
  $("pipelineStatus").textContent=count(p.count)+" asserted record(s), not verified";
  $("paymentStatus").textContent=count(o.count)+" unverified observation(s)";
  $("sandboxStatus").textContent=count((m.sandbox_payments||{}).count)+" sandbox event(s) only";
  $("opportunityStatus").textContent=count(opp.count)+" discovered (unverified)";

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
  ["metricPipeline","metricInvoices","metricObservations","metricIncome","workOpportunities","workOpps","workPlans","workQueued","workWorkflows","workOrders","workInvoices"].forEach(id => $(id).textContent="—");

  ["metricPipelineNote","metricInvoicesNote","metricObservationsNote","metricIncomeNote","pipelineStatus","paymentStatus","sandboxStatus","opportunityStatus","incomeStatus"].forEach(id => $(id).textContent="No snapshot loaded");
  ["snapshotGenerated","snapshotSchema"].forEach(id => $(id).textContent="Not loaded");
  $("snapshotMode").textContent="Waiting for a snapshot";
  $("snapshotStatus").textContent="No snapshot loaded";
  $("dashboardFootnote").textContent="No account data loaded.";
  document.querySelector(".status").textContent="NO DATA LOADED";
  $("snapshotFile").value="";
});
