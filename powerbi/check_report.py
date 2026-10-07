"""Prove the Power BI numbers: every measure evaluates, and report figures match DuckDB.

Needs the report open in its own Power BI window: powerbi/open_pbip.ps1 -Keep.
Part 1 evaluates every measure in the model and fails on any DAX error.
Part 2 runs each figure as DAX against Power BI's engine and as independent SQL
against the warehouse, and compares them. Exit code 1 on any error or mismatch.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
con = duckdb.connect(str(ROOT / "warehouse" / "revenue.duckdb"), read_only=True)


def dax_query(query):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "powerbi" / "query_desktop.ps1"), "-Dax", query],
                       capture_output=True, text=True)
    if r.returncode != 0 or "Exception" in r.stderr or not r.stdout.strip():
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout).strip() else "no output")
    row = json.loads(r.stdout.strip())
    return row[0] if isinstance(row, list) else row


def dax(expr):
    return list(dax_query(f'EVALUATE ROW("v", {expr})').values())[0]


def sql(query):
    return con.sql(query).fetchone()[0]


def measures():
    text = (ROOT / "powerbi" / "RevenueLeak.SemanticModel" / "definition" / "tables" / "Metrics.tmdl").read_text(encoding="utf-8")
    return [m.strip("'") for m in re.findall(r"^\tmeasure ('[^']+'|\S+) =", text, re.M)]


LAST = "date '2026-09-01'"
WF = "metrics.mrr_waterfall_monthly"
FY26 = "month_start between date '2025-02-01' and date '2026-01-01'"


def seg(s):
    return f'Customer[segment] = "{s}"'


def revenue_cases():
    cases = []
    for label, f, where in [("all", "", "true"), ("SMB", seg("SMB"), "segment = 'SMB'"),
                            ("Mid-Market", seg("Mid-Market"), "segment = 'Mid-Market'"),
                            ("Enterprise", seg("Enterprise"), "segment = 'Enterprise'")]:
        ff = f", {f}" if f else ""
        cases += [
            (f"{label}: MRR", f"CALCULATE([MRR]{ff})", f"select sum(ending_mrr) from {WF} where month_start = {LAST} and {where}"),
            (f"{label}: paying customers", f"CALCULATE([Paying customers]{ff})",
             f"select count(*) from {WF} where month_start = {LAST} and ending_mrr > 0 and {where}"),
            (f"{label}: new MRR", f"CALCULATE([New MRR]{ff})", f"select sum(new_mrr) from {WF} where {where}"),
            (f"{label}: expansion MRR", f"CALCULATE([Expansion MRR]{ff})", f"select sum(expansion_mrr) from {WF} where {where}"),
            (f"{label}: contraction MRR", f"CALCULATE([Contraction MRR]{ff})", f"select sum(contraction_mrr) from {WF} where {where}"),
            (f"{label}: churned MRR", f"CALCULATE([Churned MRR]{ff})", f"select sum(churned_mrr) from {WF} where {where}"),
            (f"{label}: NRR monthly", f"CALCULATE([NRR (monthly)]{ff})",
             f"select (sum(starting_mrr) + sum(expansion_mrr) - sum(contraction_mrr) - sum(churned_mrr)) / sum(starting_mrr) from {WF} where {where}"),
            (f"{label}: GRR monthly", f"CALCULATE([GRR (monthly)]{ff})",
             f"select (sum(starting_mrr) - sum(contraction_mrr) - sum(churned_mrr)) / sum(starting_mrr) from {WF} where {where}"),
            (f"{label}: logo churn monthly", f"CALCULATE([Logo churn (monthly)]{ff})",
             f"select count(*) filter (where movement_category = 'Churn') / count(*) filter (where starting_mrr > 0) from {WF} where {where}"),
            (f"{label}: NRR 12 m", f"CALCULATE([NRR (12 m)]{ff})",
             f"""select sum(coalesce(c.ending_mrr, 0)) / sum(b.ending_mrr)
                 from (select * from {WF} where {where}) b
                 left join (select * from {WF} where {where}) c
                   on c.account_id = b.account_id and c.month_start = {LAST}
                 where b.month_start = {LAST} - interval 12 month and b.ending_mrr > 0"""),
        ]
    cases += [
        ("all: NRR 12 m vs dbt revenue_retention_monthly", "[NRR (12 m)]",
         f"select nrr_t12m from metrics.revenue_retention_monthly where month_start = {LAST}"),
        ("all: MRR a year earlier", "[MRR a year earlier]", f"select sum(ending_mrr) from {WF} where month_start = {LAST} - interval 12 month"),
        ("FY2026: MRR at Jan 2026", "CALCULATE([MRR], 'Date'[fiscal_year] = 2026)",
         f"select sum(ending_mrr) from {WF} where month_start = date '2026-01-01'"),
        ("FY2026: churned MRR", "CALCULATE([Churned MRR], 'Date'[fiscal_year] = 2026)", f"select sum(churned_mrr) from {WF} where {FY26}"),
        ("FY2026 SMB: logo churn", f"CALCULATE([Logo churn (monthly)], 'Date'[fiscal_year] = 2026, {seg('SMB')})",
         f"select count(*) filter (where movement_category = 'Churn') / count(*) filter (where starting_mrr > 0) from {WF} where {FY26} and segment = 'SMB'"),
        ("ledger: net MRR movements = MRR", "SUM('MRR Movements'[mrr_delta])", f"select sum(ending_mrr) from {WF} where month_start = {LAST}"),
    ]
    return cases


def other_cases():
    usage = "core.fact_product_usage_weekly"
    last_week = f"(select max(week_start) from {usage} where week_start + interval 6 day <= (select max(usage_date) from core.fact_product_usage))"
    hl = "metrics.customer_health_weekly"
    return [
        ("cohorts: logo retention month 6", "[Month-6 logo retention]",
         "select sum(active_customers) / sum(cohort_size) from metrics.cohort_retention where months_since_signup = 6"),
        ("cohorts: revenue retention month 12", "[Month-12 revenue retention]",
         "select sum(cohort_mrr) / sum(cohort_starting_mrr) from metrics.cohort_retention where months_since_signup = 12"),
        ("cohorts: 2025-04 month 9", "CALCULATE([Logo retention], Cohorts[cohort_label] = \"2025-04\", Cohorts[months_since_signup] = 9)",
         "select logo_retention from metrics.cohort_retention where cohort_label = '2025-04' and months_since_signup = 9"),
        ("usage: weekly active users", "[Weekly active users]", f"select sum(weekly_active_users) from {usage} where week_start = {last_week}"),
        ("usage: weekly sessions per account (avg of complete weeks)", "[Sessions per account]",
         f"""select avg(s) from (select sum(sessions) / count(distinct account_id) as s from {usage}
             where week_start >= (select min(usage_date) from core.fact_product_usage)
               and week_start + interval 6 day <= (select max(usage_date) from core.fact_product_usage)
             group by week_start)"""),
        ("usage: reports per account, week of 2026-03-16", "CALCULATE([Reports per account], 'Date'[date] = DATE(2026,3,16))",
         f"select sum(reports_run) / count(distinct account_id) from {usage} where week_start = date '2026-03-16'"),
        ("support: tickets", "[Tickets]", "select count(*) from core.fact_support_tickets"),
        ("support: average CSAT", "[Average CSAT]", "select avg(csat_score) from core.fact_support_tickets"),
        ("support: SLA breached", "[Resolution SLA breached]",
         "select count(*) filter (where resolution_sla_breached) / count(resolution_hours) from core.fact_support_tickets"),
        ("support: Enterprise tickets FY2026", f"CALCULATE([Tickets], {seg('Enterprise')}, 'Date'[fiscal_year] = 2026)",
         """select count(*) from core.fact_support_tickets t join core.dim_customers c using (customer_sk)
            where c.segment = 'Enterprise' and cast(t.created_at as date) between date '2025-02-01' and date '2026-01-31'"""),
        ("health: accounts latest", "[Health accounts]", f"select count(*) from {hl} where snapshot_date = (select max(snapshot_date) from {hl})"),
        ("health: at risk latest", "[At-risk health accounts]",
         f"select count(*) from {hl} where snapshot_date = (select max(snapshot_date) from {hl}) and health_band = 'At Risk'"),
        ("risk: accounts scored", "[Accounts]", "select count(*) from ml.churn_risk_scores"),
        ("risk: high-risk accounts", "[High-risk accounts]", "select count(*) from ml.churn_risk_scores where risk_tier = 'High'"),
        ("risk: high-risk MRR", "[High-risk MRR]", "select sum(mrr) from ml.churn_risk_scores where risk_tier = 'High'"),
        ("risk: expected MRR at risk", "[Expected MRR at risk]", "select sum(mrr_at_risk) from ml.churn_risk_scores"),
        ("risk: Mid-Market expected MRR at risk", f"CALCULATE([Expected MRR at risk], {seg('Mid-Market')})",
         "select sum(mrr_at_risk) from ml.churn_risk_scores where segment = 'Mid-Market'"),
        ("risk: model lift", "[Model lift]", "select lift_top_10pct from ml.model_metrics where selected"),
        ("alerts: count", "[Leak alerts]", "select count(*) from metrics.anomaly_alerts"),
    ]


def topn_cases():
    """The report's top-N lists: right row count, and the right rows."""
    acct = sql("""select c.company_name from ml.churn_risk_scores r join core.dim_customers c using (customer_sk)
                  order by r.mrr_at_risk desc limit 1""").replace('"', '""')
    call = """FILTER(SUMMARIZECOLUMNS(Customer[company_name], 'Churn Risk'[segment], 'Churn Risk'[account_manager],
                'Churn Risk'[mrr], 'Churn Risk'[churn_probability], 'Churn Risk'[driver_1], 'Churn Risk'[mrr_at_risk],
                TREATAS({"High"}, 'Churn Risk'[risk_tier]), "r", [Call rank]), [r] <= 8)"""
    tickets = f"""FILTER(SUMMARIZECOLUMNS(Tickets[created_at], Tickets[priority], Tickets[category], Tickets[status],
                Tickets[resolution_hours], Tickets[csat_score], TREATAS({{"{acct}"}}, Customer[company_name]),
                "r", [Ticket recency]), [r] <= 6)"""
    ledger = f"""FILTER(SUMMARIZECOLUMNS('MRR Movements'[movement_date], 'MRR Movements'[movement_type],
                'MRR Movements'[change_reason], 'MRR Movements'[plan_id], 'MRR Movements'[seats],
                'MRR Movements'[mrr_before], 'MRR Movements'[mrr_after], TREATAS({{"{acct}"}}, Customer[company_name]),
                "r", [Ledger recency]), [r] <= 6)"""
    acct_sql = acct.replace('""', '"').replace("'", "''")
    return [
        ("call list: rows shown", f"COUNTROWS({call})", "select 8"),
        ("call list: expected loss of the 8 shown", f"SUMX({call}, 'Churn Risk'[mrr_at_risk])",
         "select sum(mrr_at_risk) from (select mrr_at_risk from ml.churn_risk_scores where risk_tier = 'High' order by mrr_at_risk desc limit 8)"),
        ("account tickets: rows shown", f"COUNTROWS({tickets})",
         f"""select least(6, count(*)) from core.fact_support_tickets t join core.dim_customers c using (customer_sk)
             where c.company_name = '{acct_sql}'"""),
        ("account ledger: rows shown", f"COUNTROWS({ledger})",
         f"""select least(6, count(*)) from core.fact_mrr_movements m join core.dim_customers c using (customer_sk)
             where c.company_name = '{acct_sql}'"""),
    ]


def main():
    bad = 0
    names = measures()
    print(f"Part 1: evaluating {len(names)} measures")
    for m in names:
        try:
            dax(f"[{m}]")
        except Exception as e:  # noqa: BLE001
            bad += 1
            print(f"  ERROR  {m}: {e}")
    print(f"  {len(names) - bad} of {len(names)} measures evaluate")

    cases = revenue_cases() + other_cases() + topn_cases()
    print(f"Part 2: {len(cases)} report numbers vs DuckDB")
    diffs = 0
    for name, d, q in cases:
        a, b = dax(d), sql(q)
        same = (a is None and b is None) or (a is not None and b is not None and abs(float(a) - float(b)) < 1e-6 * max(1, abs(float(b))))
        diffs += not same
        print(f"  {'ok  ' if same else 'DIFF'}  {name:52s} Power BI {a!s:>22}   DuckDB {b!s:>22}")
    print(f"  {len(cases) - diffs} of {len(cases)} match")
    sys.exit(1 if bad or diffs else 0)


if __name__ == "__main__":
    main()
