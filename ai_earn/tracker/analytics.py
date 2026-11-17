"""Analytics handlers (42-45) — extracted."""
from .helpers import _read


def handle_summary(analytics, token, stdin, out):
    from_ts = _read(stdin, out, "From unix ts (0 for all time): ").strip()
    to_ts = _read(stdin, out, "To unix ts (empty for now): ").strip()
    try:
        fts = int(from_ts) if from_ts else 0
        tts = int(to_ts) if to_ts else 4102444800
        summ = analytics.summary(token, from_ts=fts, to_ts=tts)
        out.write(f"Analytics summary {fts}->{tts} generated {summ['funnel']['generated_at']}\n")
        out.write(f"Funnel opp {summ['funnel'].get('opportunities', {}).get('total',0)} cust {summ['funnel'].get('customers', {}).get('total',0)} invoices {summ['funnel'].get('invoices', {}).get('total',0)}\n")
        out.write(f"Conversion opp vetted {summ['conversion'].get('opportunities', {}).get('vetted_rate',0):.2f} cust converted {summ['conversion'].get('customers', {}).get('converted_over_lead',0):.2f}\n")
        out.write(f"MRR total {summ['mrr']['total_paise']/100:.2f} INR over {summ['mrr']['months_with_income']} months avg {summ['mrr']['average_per_month_paise']/100:.2f}\n")
        out.write(f"Workload queued {summ['workload'].get('queued_tasks',0)} pending follow {summ['workload'].get('pending_followups',0)} net {summ['workload'].get('ledger_net_paise',0)/100:.2f}\n")
        out.write(f"Verification: {summ['funnel']['verification']} / {summ['mrr']['verification']} — no fabricated data.\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_funnel(analytics, token, stdin, out):
    from_ts = _read(stdin, out, "From unix ts (0): ").strip()
    to_ts = _read(stdin, out, "To unix ts (empty for max): ").strip()
    try:
        fts = int(from_ts) if from_ts else 0
        tts = int(to_ts) if to_ts else 4102444800
        fun = analytics.funnel(token, from_ts=fts, to_ts=tts)
        out.write(f"Funnel {fts}->{tts}:\n")
        for k in ("opportunities","customers","invoices","provider_orders","services","communications"):
            if k in fun:
                out.write(f" {k}: {fun[k].get('total',0)} by {fun[k].get('by_status', fun[k].get('by_kind', {}))}\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_mrr(analytics, token, stdin, out):
    from_ts = _read(stdin, out, "From unix ts (0): ").strip()
    to_ts = _read(stdin, out, "To unix ts (empty for max): ").strip()
    try:
        fts = int(from_ts) if from_ts else 0
        tts = int(to_ts) if to_ts else 4102444800
        mrr = analytics.mrr(token, from_ts=fts, to_ts=tts)
        out.write(f"MRR {fts}->{tts} total {mrr['total_paise']/100:.2f} INR months {mrr['months_with_income']} avg {mrr['average_per_month_paise']/100:.2f}\n")
        for mon, amt in sorted(mrr['monthly_buckets'].items()):
            out.write(f" {mon}: {amt/100:.2f}\n")
        out.write(mrr['note'] + "\n")
    except Exception as e:
        out.write(str(e) + "\n")


def handle_export_analytics(analytics, token, stdin, out):
    from_ts = _read(stdin, out, "From unix ts (0): ").strip()
    to_ts = _read(stdin, out, "To unix ts (empty for max): ").strip()
    try:
        fts = int(from_ts) if from_ts else 0
        tts = int(to_ts) if to_ts else 4102444800
        csv_text = analytics.export_csv(token, from_ts=fts, to_ts=tts)
        out.write("CSV export (first 2000 chars):\n")
        out.write(csv_text[:2000] + ("...\n" if len(csv_text)>2000 else "\n"))
        out.write(f"Total {len(csv_text)} chars, {csv_text.count(chr(10))} lines.\n")
    except Exception as e:
        out.write(str(e) + "\n")
