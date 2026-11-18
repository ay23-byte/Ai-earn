// Accessibility audit (P4) — axe-core in jsdom. Optional tooling, not a
// runtime dependency. Setup (outside the repo):
//   mkdir -p /tmp/axe-audit && cd /tmp/axe-audit && npm init -y
//   npm install axe-core jsdom
//   AXE_HOME=/tmp/axe-audit node --experimental-vm-modules tools/accessibility_audit.mjs dashboard/index.html dashboard/app.html dashboard/connected.html
// Exits 1 on any critical/serious violation. Color-contrast is reported
// "incomplete" under jsdom (no canvas); verify contrast in a real browser.
import { readFileSync } from "node:fs";
const axeHome = process.env.AXE_HOME || "/tmp/axe-audit";
const { JSDOM } = await import(axeHome + "/node_modules/jsdom/lib/api.js");

const pages = process.argv.slice(2);
let failed = false;
for (const page of pages) {
  const html = readFileSync(page, "utf8");
  const dom = new JSDOM(html, { url: "http://127.0.0.1:8000/", runScripts: "outside-only", pretendToBeVisual: true });
  dom.window.eval(readFileSync(axeHome + "/node_modules/axe-core/axe.min.js", "utf8"));
  const results = await dom.window.axe.run(dom.window.document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } });
  const violations = results.violations.map(v => ({ id: v.id, impact: v.impact, nodes: v.nodes.length, help: v.help, targets: v.nodes.slice(0,3).map(n => n.target.join(" ")) }));
  console.log(`\n== ${page}: ${violations.length} violation rule(s), ${results.passes.length} passes, ${results.incomplete.length} incomplete [${results.incomplete.map(i=>i.id).join(",")}]`);
  for (const v of violations) { console.log(`  [${v.impact}] ${v.id} x${v.nodes} — ${v.help} :: ${v.targets.join(" | ")}`); if (["critical","serious"].includes(v.impact)) failed = true; }
}
process.exit(failed ? 1 : 0);
