"""Fill the case study with numbers computed from the warehouse.

    python docs/render_docs.py
reads docs/case-study.template.html, replaces every {{token}} with a figure
queried from DuckDB, the scorecard or the multi-seed results, and writes
docs/case-study.html. Nothing in the case study is typed by hand, so a rerun of
the pipeline cannot leave a stale number behind. Fails if any token is unfilled.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"


DQ_LABELS = {
    "INC-04_duplicate_webhook_events": "Stripe webhooks re-sent under new ids (INC-04)",
    "stripe_exact_retry_rows": "Stripe events delivered twice, same id",
    "stripe_plan_id_casing_whitespace": "Plan ids with stray casing or spaces",
    "stripe_customer_missing_account_id": "Stripe customers without an account id",
    "ticket_null_account_id": "Tickets without an account",
    "ticket_timestamps_in_ist_offset": "Ticket times in IST instead of UTC",
    "ticket_solved_before_created": "Tickets solved before they were opened",
    "ticket_csat_out_of_range": "CSAT scores outside 1-5",
    "ticket_priority_casing": "Ticket priorities with stray casing",
    "telemetry_null_session_end": "Sessions with no end time",
    "telemetry_end_before_start": "Sessions ending before they start",
    "telemetry_exact_duplicate_rows": "Sessions logged twice",
    "crm_changes_recorded_30plus_days_late": "CRM changes recorded 30+ days late",
}
FEATURE_LABELS = {
    "days_to_renewal": "days to renewal", "seats": "seat count", "adoption_rate_30d": "seat adoption",
    "billing_interval_year": "annual billing", "billing_interval_month": "monthly billing",
    "csat_90d": "CSAT", "usage_velocity_delta": "usage velocity", "tenure_days": "tenure",
    "price_increase_90d": "a recent price rise", "ticket_spike": "ticket spikes",
}


def money(v, unit=""):
    if unit == "M":
        return f"${v / 1e6:.2f}M"
    if unit == "K":
        return f"${v / 1e3:,.0f}K"
    return f"${v:,.0f}"


def pct(v, digits=0):
    return f"{v * 100:.{digits}f}%"


def md_table_to_html(path: Path, label_cols: dict) -> str:
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("|")]
    head = [c.strip() for c in lines[0].strip("|").split("|")]
    rows = [[c.strip() for c in l.strip("|").split("|")] for l in lines[2:]]
    keep = [i for i, h in enumerate(head) if h in label_cols]
    out = ["<table>", "<tr>" + "".join(f"<th>{label_cols[head[i]]}</th>" for i in keep) + "</tr>"]
    for r in rows:
        cells = []
        for i in keep:
            v = r[i]
            if head[i] == "model":
                v = "logistic" if v.startswith("logistic") else v
            cells.append(f'<td class="num">{v}</td>' if head[i] != "seed" else f"<td>{v}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table>")
    return "\n".join(out)


def main():
    con = duckdb.connect(str(ROOT / "warehouse" / "revenue.duckdb"), read_only=True)
    q = lambda s: con.sql(s).fetchone()
    key = json.loads((ROOT / "data" / "answer_key" / "answer_key.json").read_text())
    t = {}

    # ---------------------------------------------------------------- the business
    t["accounts"] = f"{q('select count(distinct account_id) from core.dim_customers where customer_sk > 0')[0]:,}"
    paying = q("select count(*) from metrics.mrr_waterfall_monthly where month_start = date '2026-09-01' and ending_mrr > 0")[0]
    t["paying_end"] = f"{paying:,}"
    mrr_end, mrr_prev = q("""select ending_mrr, (select ending_mrr from metrics.revenue_retention_monthly where month_start = date '2025-09-01')
                             from metrics.revenue_retention_monthly where month_start = date '2026-09-01'""")
    t["mrr_end"], t["arr_end"] = money(mrr_end, "M"), money(mrr_end * 12, "M")
    t["mrr_growth"] = pct(mrr_end / mrr_prev - 1)
    t["nrr_t12m"], t["grr_t12m"] = [pct(v, 1) for v in q("select nrr_t12m, grr_t12m from metrics.revenue_retention_monthly where month_start = date '2026-09-01'")]
    churn_avg, grr_avg = q("""select avg(logo_churn_rate), avg(grr_mom) from metrics.revenue_retention_monthly
                              where month_start >= date '2025-10-01'""")
    t["logo_churn_avg"], t["grr_mom_avg"] = pct(churn_avg, 1), pct(grr_avg, 1)
    churns = q("select count(*) from core.fact_mrr_movements where movement_type = 'Churn'")[0]
    t["churns_total"] = f"{churns:,}"
    lost, exp = q("select sum(churned_mrr + contraction_mrr), sum(expansion_mrr) from metrics.mrr_waterfall_monthly")
    t["lost_mrr"], t["expansion_mrr"] = money(lost, "K"), money(exp, "K")
    t["tickets"] = f"{q('select count(*) from core.fact_support_tickets')[0]:,}"
    t["sessions"] = f"{q('select sum(sessions) from core.fact_product_usage')[0] / 1e6:.1f} million"
    t["dirty_rows"] = f"{sum(key['data_quality_injections'].values()):,}"

    # ---------------------------------------------------------------- what the data says (per incident)
    smb = con.sql("""select month_start, count(*) filter (where movement_category = 'Churn') * 1.0 / count(*) filter (where starting_mrr > 0) r
                     from metrics.mrr_waterfall_monthly where plan_id = 'starter' group by 1 order by 1""").df().set_index("month_start")["r"]
    t["starter_churn_before"] = pct(smb.loc["2025-04-01":"2025-10-01"].mean(), 1)
    t["starter_churn_after"] = pct(smb.loc["2025-11-01":"2026-01-01"].mean(), 1)
    bug = con.sql("""select cast(date_trunc('week', created_at) as date) w, count(*) n from core.fact_support_tickets
                     where category = 'bug' group by 1 order by 1""").df().set_index("w")["n"]
    t["bug_peak"] = f"{int(bug.loc['2026-03-09':'2026-03-30'].max())}"
    t["bug_normal"] = f"{bug.loc['2025-12-01':'2026-03-02'].mean():.0f}"
    rep = con.sql("""select week_start, sum(reports_run) * 1.0 / count(distinct account_id) v from core.fact_product_usage_weekly
                     group by 1 order by 1""").df().set_index("week_start")["v"]
    t["reports_drop"] = pct(1 - rep.loc["2026-03-16":"2026-03-23"].mean() / rep.loc["2026-01-05":"2026-03-02"].mean())
    am = con.sql("""select detail from metrics.anomaly_alerts where detector = 'am_book_retention' order by detected_at desc limit 1""").fetchone()
    t["am_alert"] = am[0] if am else "no alert"
    t["am_name"] = key["planted_am_neglect_name"]
    coh = con.sql("""select sum(active_customers) filter (where cohort_label in ('2025-04', '2025-05')) * 1.0
                          / sum(cohort_size) filter (where cohort_label in ('2025-04', '2025-05')),
                            sum(active_customers) filter (where cohort_label not in ('2025-04', '2025-05')) * 1.0
                          / sum(cohort_size) filter (where cohort_label not in ('2025-04', '2025-05'))
                     from metrics.cohort_retention where months_since_signup = 9""").fetchone()
    t["cohort_weak"], t["cohort_rest"] = pct(coh[0]), pct(coh[1])
    dups, dup_mrr = q("""select sum(duplicate_deliveries_removed),
                                sum(duplicate_deliveries_removed * (mrr_cents - coalesce(previous_mrr_cents, 0))) / 100.0
                         from intermediate.int_subscription_events where duplicate_deliveries_removed > 0""")
    t["dup_events"], t["dup_mrr"] = f"{int(dups)}", money(dup_mrr)

    # ---------------------------------------------------------------- scorecard
    inc = con.sql("select * from scorecard.incidents order by incident").df()
    fa = con.sql("select * from scorecard.false_alarms").df()
    t["incidents_found"] = f"{int(inc['detected'].sum())} of {len(inc)}"
    t["false_alarms"] = f"{int(fa['related_incident'].isna().sum())}"
    t["secondary"] = f"{int(fa['related_incident'].notna().sum())}"
    names = {"price_hike": "Starter price rise", "bad_release": "Release 5.2 breaks Reports",
             "am_neglect": "Neglected account-manager book", "duplicate_webhooks": "Stripe re-sends webhooks",
             "partner_cohort": "Weak partner cohort"}
    rows = []
    for r in inc.itertuples():
        days = "" if r.days_to_detect != r.days_to_detect else f"{int(r.days_to_detect)}"
        rows.append(f"<tr><td>{r.incident}</td><td>{names[r.kind]}</td><td class=\"date\">{str(r.started)[:10]}</td>"
                    f"<td class=\"num\">{days or 'missed'}</td><td>{r.detector or ''}</td><td>{r.evidence or ''}</td></tr>")
    t["incident_rows"] = "\n".join(rows)
    cols = {"seed": "Seed", "INC-01": "INC-01", "INC-02": "INC-02", "INC-03": "INC-03", "INC-04": "INC-04",
            "INC-05": "INC-05", "false_alarms": "False alarms"}
    t["seeds_v1"] = md_table_to_html(DOCS / "multi_seed_v1.md", cols)
    t["seeds_v2"] = md_table_to_html(DOCS / "multi_seed_v2.md", cols)

    dq = con.sql("select * from scorecard.data_quality").df()
    t["dq_types"] = f"{len(dq)}"
    # a type counts as caught when the tests flag at least 99% of it; the rest are rows hit by two injections
    t["dq_full"] = f"{int((dq['recall'] >= 0.99).sum())}"
    t["dq_rows"] = "\n".join(
        f"<tr><td>{DQ_LABELS.get(r.issue, r.issue)}</td><td class=\"num\">{r.injected:,}</td><td class=\"num\">{r.flagged:,}</td>"
        f"<td>{r.handling}</td></tr>" for r in dq.itertuples())

    m = con.sql("select * from ml.model_metrics where selected").df().iloc[0]
    other = con.sql("select * from ml.model_metrics where not selected").df().iloc[0]
    t["model"] = "logistic regression" if m["model"].startswith("logistic") else "XGBoost"
    t["other_model"] = "XGBoost" if t["model"] == "logistic regression" else "logistic regression"
    t["roc"], t["pr"] = f"{m['roc_auc']:.2f}", f"{m['pr_auc']:.2f}"
    t["other_roc"], t["other_pr"] = f"{other['roc_auc']:.2f}", f"{other['pr_auc']:.2f}"
    t["base_rate"], t["prec10"] = pct(m["base_rate"], 1), pct(m["precision_top_10pct"], 1)
    t["lift"], t["recall10"] = f"{m['lift_top_10pct']:.1f}", pct(m["recall_top_10pct"])
    ew = con.sql("select * from scorecard.early_warning").df().set_index("true_cause")
    t["ew_churns"] = f"{int(ew.loc['ALL', 'churns'])}"
    t["ew_model"], t["ew_health"] = pct(ew.loc["ALL", "model_caught"]), pct(ew.loc["ALL", "health_caught"])
    t["ew_lead"] = f"{int(ew.loc['ALL', 'model_median_lead_days'])}"
    t["ew_organic"] = pct(ew.loc["organic", "model_caught"]) if "organic" in ew.index else "n/a"
    hr = q("select count(*), sum(mrr), sum(mrr_at_risk) from ml.churn_risk_scores where risk_tier = 'High'")
    t["high_n"], t["high_mrr"], t["high_loss"] = f"{hr[0]}", money(hr[1], "K"), money(hr[2], "K")
    top = con.sql("select feature from ml.feature_importance limit 5").df()["feature"].tolist()
    t["top_features"] = ", ".join(FEATURE_LABELS.get(f, f.replace("_", " ")) for f in top)

    # ---------------------------------------------------------------- proof
    rr = json.loads((ROOT / "dbt" / "target" / "run_results.json").read_text())["results"]
    t["dbt_nodes"] = f"{len(rr)}"
    t["dbt_warn"] = f"{sum(r['status'] == 'warn' for r in rr)}"

    html = (DOCS / "case-study.template.html").read_text(encoding="utf-8")
    missing = sorted(set(re.findall(r"\{\{(\w+)\}\}", html)) - set(t))
    if missing:
        raise SystemExit(f"unfilled tokens: {missing}")
    html = re.sub(r"\{\{(\w+)\}\}", lambda mt: str(t[mt.group(1)]), html)
    (DOCS / "case-study.html").write_text(html, encoding="utf-8", newline="\n")
    print(f"wrote docs/case-study.html ({len(t)} figures from the warehouse)")
    for k in ("mrr_end", "nrr_t12m", "incidents_found", "false_alarms", "roc", "lift", "ew_model", "ew_lead"):
        print(f"  {k:16s} {t[k]}")


if __name__ == "__main__":
    main()
