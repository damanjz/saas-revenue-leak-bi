"""Generate the Power BI project (PBIP): TMDL semantic model + PBIR report.

The model imports the CSVs written by scripts/export_marts.py. Column types come
from the same DuckDB queries, so re-running the pipeline and this script keeps
the two in sync.

Three pages:
  Revenue    executive MRR growth engine: MRR, ARR, NRR/GRR, monthly movements
  Cohorts    signup-cohort retention heatmap and product adoption health
  Accounts   proactive customer success: churn risk x health action matrix, action list, leak alerts
"""
import argparse
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from export_marts import EXPORTS  # noqa: E402

OUT = ROOT / "powerbi"
NAME = "RevenueLeak"
SM = OUT / f"{NAME}.SemanticModel"
RP = OUT / f"{NAME}.Report"
DATA_FOLDER = str(ROOT / "exports") + "\\"
PORTABLE_DATA_FOLDER = "C:\\saas-revenue-leak\\exports\\"

SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition"

# ---------------------------------------------------------------- palette (same editorial system as the other BI pieces)
INK, OCHRE, TEAL, RUST, PLUM = "#2f55b0", "#c27a1a", "#1f8f7a", "#c0533a", "#8456b0"
PAPER, RAIL, RULE = "#fbfaf7", "#f2efe7", "#e3ded2"
TEXT, MUTED = "#1d2330", "#5b6170"
SERIF, SANS = "Georgia", "Segoe UI"
RAMP = ["#f2efe7", "#dfe2ea", "#c6cee2", "#a6b5d8", "#8299cb", "#5d7cbe", "#3f62b4", "#2a4a96"]
LOSS_RAMP = ["#f2efe7", "#f0e0d6", "#ebcbbb", "#e3b09a", "#d99478", "#cd7558", "#c0533a", "#9c3f2a"]

# ---------------------------------------------------------------- model
TABLES = {  # model table -> (export name, visible columns)
    "Date": ("dim_date", {"date", "month_start", "year", "year_quarter", "fiscal_year", "week_start"}),
    "Customer": ("dim_customers", {"company_name", "segment", "account_manager", "region", "arr_band", "cs_tier",
                                   "acquisition_channel", "industry"}),
    "MRR Movements": ("fact_mrr_movements", {"movement_date", "movement_type", "change_reason", "plan_id", "seats",
                                             "mrr_before", "mrr_after"}),
    "Usage Weekly": ("fact_product_usage_weekly", set()),
    "Tickets": ("fact_support_tickets", {"created_at", "priority", "category", "status", "csat_score",
                                         "resolution_hours"}),
    "Waterfall": ("mrr_waterfall_monthly", set()),
    "Cohorts": ("cohort_retention", {"cohort_label", "months_since_signup"}),
    "Health": ("customer_health_weekly", {"health_band"}),
    "Alerts": ("anomaly_alerts", {"detected_at", "detector", "detail"}),
    "Churn Risk": ("churn_risk_scores", {"segment", "account_manager", "mrr", "churn_probability", "risk_tier",
                                         "health_band", "driver_1", "driver_2", "driver_3", "mrr_at_risk"}),
    "Drivers": ("feature_importance", {"feature_label"}),
    "Model Metrics": ("model_metrics", set()),
}
SORT_BY = {("Churn Risk", "risk_tier"): "risk_tier_order", ("Churn Risk", "health_band"): "health_band_order",
           ("Health", "health_band"): "health_band_order", ("Cohorts", "cohort_label"): "cohort_month"}
RELATIONSHIPS = [
    ("MRR Movements", "date_key", "Date", "date_key"), ("MRR Movements", "customer_sk", "Customer", "customer_sk"),
    ("Usage Weekly", "week_start_date_key", "Date", "date_key"), ("Usage Weekly", "customer_sk", "Customer", "customer_sk"),
    ("Tickets", "created_date_key", "Date", "date_key"), ("Tickets", "customer_sk", "Customer", "customer_sk"),
    ("Waterfall", "month_date_key", "Date", "date_key"), ("Waterfall", "customer_sk", "Customer", "customer_sk"),
    ("Health", "snapshot_date_key", "Date", "date_key"), ("Health", "customer_sk", "Customer", "customer_sk"),
    ("Alerts", "period_date_key", "Date", "date_key"),
    ("Churn Risk", "customer_sk", "Customer", "customer_sk"),
]

USD = "\\$#,0"
PCT = "0.0%"
LAST_MONTH = "VAR m = CALCULATE ( MAX ( Waterfall[month_start] ) )"


def ramp(value, lo, hi, colors=RAMP):
    """DAX: pick a colour by where value sits between lo and hi."""
    cases = ", ".join(f'i = {i}, "{c}"' for i, c in enumerate(colors))
    return f"""
        VAR i = IF ( ISBLANK ( {value} ), BLANK (), INT ( MIN ( {len(colors) - 1}, MAX ( 0, DIVIDE ( {value} - {lo}, {hi} - {lo} ) * {len(colors)} ) ) ) )
        RETURN SWITCH ( TRUE (), ISBLANK ( i ), "{PAPER}", {cases} )"""


MEASURES = [
    # ---------------- Revenue
    ("MRR", f"""
        {LAST_MONTH}
        RETURN CALCULATE ( SUM ( Waterfall[ending_mrr] ), Waterfall[month_start] = m )""", USD,
     "Monthly recurring revenue at the end of the last month in context."),
    ("ARR", "[MRR] * 12", USD, "Annual run rate: MRR x 12."),
    ("MRR ($M)", "DIVIDE ( [MRR], 1e6 )", r'\$0.00"M"', "MRR in millions, for cards (locale-independent)."),
    ("ARR ($M)", "DIVIDE ( [ARR], 1e6 )", r'\$0.0"M"', "ARR in millions, for cards (locale-independent)."),
    ("MRR a year earlier", f"""
        {LAST_MONTH}
        RETURN CALCULATE ( SUM ( Waterfall[ending_mrr] ), REMOVEFILTERS ( 'Date' ), Waterfall[month_start] = EDATE ( m, -12 ) )""",
     USD, "MRR twelve months before the last month in context."),
    ("MRR growth (12 m)", "DIVIDE ( [MRR], [MRR a year earlier] ) - 1", PCT, "MRR change over twelve months."),
    ("Paying customers", f"""
        {LAST_MONTH}
        RETURN CALCULATE ( COUNTROWS ( Waterfall ), Waterfall[month_start] = m, Waterfall[ending_mrr] > 0 )""", "#,0",
     "Accounts with MRR at the end of the last month in context."),
    ("ARPA", "DIVIDE ( [MRR], [Paying customers] )", USD, "Average MRR per paying account."),
    ("New MRR", "SUM ( Waterfall[new_mrr] )", USD, "MRR from first-time customers in the period."),
    ("Expansion MRR", "SUM ( Waterfall[expansion_mrr] )", USD, "MRR added by existing customers (seats, upgrades, price)."),
    ("Reactivation MRR", "SUM ( Waterfall[reactivation_mrr] )", USD, "MRR from returning customers."),
    ("Contraction MRR", "SUM ( Waterfall[contraction_mrr] )", USD, "MRR lost to downgrades and seat cuts."),
    ("Churned MRR", "SUM ( Waterfall[churned_mrr] )", USD, "MRR lost to cancellations."),
    ("Contraction", "-[Contraction MRR]", USD, "Contraction as a negative bar."),
    ("Churn", "-[Churned MRR]", USD, "Churn as a negative bar."),
    ("New", "[New MRR]", USD, "New MRR (chart label)."),
    ("Expansion", "[Expansion MRR]", USD, "Expansion MRR (chart label)."),
    ("Reactivation", "[Reactivation MRR]", USD, "Reactivation MRR (chart label)."),
    ("Net new MRR", "[New MRR] + [Expansion MRR] + [Reactivation MRR] - [Contraction MRR] - [Churned MRR]", USD,
     "All MRR movements in the period, netted."),
    ("Lost MRR", "[Contraction MRR] + [Churned MRR]", USD, "MRR lost to downgrades and cancellations."),
    ("NRR (monthly)", """
        VAR s = SUM ( Waterfall[starting_mrr] )
        RETURN DIVIDE ( s + [Expansion MRR] - [Contraction MRR] - [Churned MRR], s )""", PCT,
     "Net revenue retention, month over month: kept plus expanded MRR from customers paying at the start of each month."),
    ("GRR (monthly)", """
        VAR s = SUM ( Waterfall[starting_mrr] )
        RETURN DIVIDE ( s - [Contraction MRR] - [Churned MRR], s )""", PCT,
     "Gross revenue retention, month over month: MRR kept before any expansion."),
    ("NRR (12 m)", f"""
        {LAST_MONTH}
        VAR base =
            CALCULATETABLE (
                VALUES ( Waterfall[account_id] ),
                REMOVEFILTERS ( 'Date' ), Waterfall[month_start] = EDATE ( m, -12 ), Waterfall[ending_mrr] > 0
            )
        VAR mrr_then =
            CALCULATE ( SUM ( Waterfall[ending_mrr] ), REMOVEFILTERS ( 'Date' ), Waterfall[month_start] = EDATE ( m, -12 ),
                        Waterfall[ending_mrr] > 0 )
        VAR mrr_now =
            CALCULATE ( SUM ( Waterfall[ending_mrr] ), REMOVEFILTERS ( 'Date' ), Waterfall[month_start] = m,
                        TREATAS ( base, Waterfall[account_id] ) )
        RETURN DIVIDE ( mrr_now, mrr_then )""", PCT,
     "Trailing-12-month net revenue retention: MRR today from accounts that paid a year ago, over their MRR then."),
    ("Logo churn (monthly)", """
        DIVIDE (
            CALCULATE ( COUNTROWS ( Waterfall ), Waterfall[movement_category] = "Churn" ),
            CALCULATE ( COUNTROWS ( Waterfall ), Waterfall[starting_mrr] > 0 )
        )""", PCT, "Share of paying customers who cancel in a month, averaged over the period."),
    ("Insight: revenue", f"""
        {LAST_MONTH}
        RETURN
            FORMAT ( m, "mmm yyyy" ) & ": MRR " & FORMAT ( [MRR] / 1e6, "$0.00" ) & "M, "
                & FORMAT ( [MRR growth (12 m)], "+0%;-0%" ) & " in a year. Churn and downgrades took "
                & FORMAT ( [Lost MRR] / 1e3, "$#,0" ) & "K; expansion won back " & FORMAT ( [Expansion MRR] / 1e3, "$#,0" ) & "K." """,
     None, "One-line reading of the revenue page."),

    # ---------------- Cohorts and adoption
    ("Logo retention", "DIVIDE ( SUM ( Cohorts[active_customers] ), SUM ( Cohorts[cohort_size] ) )", "0%",
     "Share of a cohort's customers still paying."),
    ("Revenue retention", "DIVIDE ( SUM ( Cohorts[cohort_mrr] ), SUM ( Cohorts[cohort_starting_mrr] ) )", "0%",
     "Cohort MRR now over its first-month MRR."),
    ("Month-6 logo retention", "CALCULATE ( [Logo retention], Cohorts[months_since_signup] = 6 )", "0%",
     "Share of customers still paying six months after signing up, across cohorts."),
    ("Month-12 revenue retention", "CALCULATE ( [Revenue retention], Cohorts[months_since_signup] = 12 )", "0%",
     "Cohort MRR twelve months in, over first-month MRR, across cohorts."),
    ("Cohort cell", 'FORMAT ( [Logo retention] * 100, "0" )', None, "Retention as a whole number for the heatmap."),
    ("Cohort colour", ramp("( 1 - [Logo retention] )", 0, 0.45, LOSS_RAMP), None,
     "Heatmap colour: darker means more of the cohort lost."),
    ("Cohort text colour", f"""
        IF ( ISBLANK ( [Logo retention] ), "{TEXT}", IF ( 1 - [Logo retention] >= 0.45 * 5 / 8, "{PAPER}", "{TEXT}" ) )""",
     None, "Light text on the darkest heatmap cells so every number stays readable."),
    ("Weekly active users", """
        VAR w = CALCULATE ( MAX ( 'Usage Weekly'[week_start] ), 'Usage Weekly'[is_complete_week] = TRUE () )
        RETURN CALCULATE ( SUM ( 'Usage Weekly'[weekly_active_users] ), 'Usage Weekly'[week_start] = w )""", "#,0",
     "Distinct users active in the last complete week in context."),
    ("Sessions per account", """
        AVERAGEX (
            CALCULATETABLE ( VALUES ( 'Usage Weekly'[week_start] ), 'Usage Weekly'[is_complete_week] = TRUE () ),
            CALCULATE ( DIVIDE ( SUM ( 'Usage Weekly'[sessions] ), DISTINCTCOUNT ( 'Usage Weekly'[account_id] ) ) )
        )""", "0.0",
     "Sessions per active account in a week, averaged over the complete weeks in context."),
    ("Reports per account", """
        AVERAGEX (
            CALCULATETABLE ( VALUES ( 'Usage Weekly'[week_start] ), 'Usage Weekly'[is_complete_week] = TRUE () ),
            CALCULATE ( DIVIDE ( SUM ( 'Usage Weekly'[reports_run] ), DISTINCTCOUNT ( 'Usage Weekly'[account_id] ) ) )
        )""", "0.0",
     "Reports run per active account in a week, averaged over the complete weeks in context."),
    ("Dashboards per account", """
        AVERAGEX (
            CALCULATETABLE ( VALUES ( 'Usage Weekly'[week_start] ), 'Usage Weekly'[is_complete_week] = TRUE () ),
            CALCULATE ( DIVIDE ( SUM ( 'Usage Weekly'[dashboards_viewed] ), DISTINCTCOUNT ( 'Usage Weekly'[account_id] ) ) )
        )""", "0.0",
     "Dashboards viewed per active account in a week, averaged over the complete weeks in context."),
    ("Tickets", "COUNTROWS ( Tickets )", "#,0", "Support tickets created."),
    ("Average CSAT", "AVERAGE ( Tickets[csat_score] )", "0.00", "Average satisfaction score, 1 to 5."),
    ("Resolution SLA breached", """
        DIVIDE (
            CALCULATE ( COUNTROWS ( Tickets ), Tickets[resolution_sla_breached] = TRUE () ),
            CALCULATE ( COUNTROWS ( Tickets ), NOT ISBLANK ( Tickets[resolution_hours] ) )
        )""", PCT, "Share of solved tickets resolved later than the priority's SLA."),
    ("Health accounts", """
        VAR d = CALCULATE ( MAX ( Health[snapshot_date] ) )
        RETURN CALCULATE ( COUNTROWS ( Health ), Health[snapshot_date] = d )""", "#,0",
     "Paying accounts in the latest weekly health snapshot in context."),
    ("Insight: cohorts", """
        VAR weak = CALCULATE ( [Logo retention], Cohorts[months_since_signup] = 9, Cohorts[cohort_label] IN { "2025-04", "2025-05" } )
        VAR rest = CALCULATE ( [Logo retention], Cohorts[months_since_signup] = 9, NOT Cohorts[cohort_label] IN { "2025-04", "2025-05" } )
        RETURN
            FORMAT ( [Month-6 logo retention], "0%" ) & " still pay at month 6. Apr-May 2025 cohorts: "
                & FORMAT ( weak, "0%" ) & " at month 9, vs " & FORMAT ( rest, "0%" ) & " for the rest." """, None,
     "One-line reading of the cohort page."),

    # ---------------- Accounts (customer success)
    ("Accounts", "COUNTROWS ( 'Churn Risk' )", "#,0", "Active accounts scored by the churn model."),
    ("Book MRR", "SUM ( 'Churn Risk'[mrr] )", USD, "MRR of the scored accounts."),
    ("High-risk MRR ($K)", "DIVIDE ( [High-risk MRR], 1e3 )", r'\$0.0"K"', "High-risk MRR in thousands, for cards."),
    ("Expected MRR at risk ($K)", "DIVIDE ( [Expected MRR at risk], 1e3 )", r'\$0.0"K"', "Expected loss in thousands, for cards."),
    ("High-risk accounts", "CALCULATE ( COUNTROWS ( 'Churn Risk' ), 'Churn Risk'[risk_tier] = \"High\" )", "#,0",
     "Accounts in the riskiest 10% of the book."),
    ("High-risk MRR", "CALCULATE ( SUM ( 'Churn Risk'[mrr] ), 'Churn Risk'[risk_tier] = \"High\" )", USD,
     "MRR held by the riskiest 10% of accounts."),
    ("Expected MRR at risk", "SUM ( 'Churn Risk'[mrr_at_risk] )", USD,
     "MRR x 60-day churn probability, summed: the MRR the model expects to lose."),
    ("At-risk health accounts", """
        VAR d = CALCULATE ( MAX ( Health[snapshot_date] ) )
        RETURN CALCULATE ( COUNTROWS ( Health ), Health[snapshot_date] = d, Health[health_band] = "At Risk" ) + 0""", "#,0",
     "Accounts in the At Risk health band in the latest snapshot."),
    ("Model lift", "CALCULATE ( MAX ( 'Model Metrics'[lift_top_10pct] ), 'Model Metrics'[selected] = TRUE () )", '0.0"x"',
     "How much more often the top 10% flagged accounts churned than average, out of time."),
    ("Matrix cell", 'FORMAT ( [Book MRR] / 1e3, "$0" ) & "K  ·  " & FORMAT ( [Accounts], "0" )', None,
     "MRR and account count for a cell of the action matrix."),
    ("Matrix colour", ramp("[Book MRR]", 0, 450000), None, "Action matrix colour by MRR."),
    ("Matrix text colour", f"""
        IF ( [Book MRR] >= 450000 * 5 / 8, "{PAPER}", "{TEXT}" )""", None,
     "Light text on the darkest action-matrix cells."),
    ("Call rank", """
        -- rank against every row the visual could show: clear the row's own column filters
        -- (ALLSELECTED on the table) and the company-name row filter that reaches it through Customer
        IF (
            HASONEVALUE ( 'Churn Risk'[account_id] ),
            RANKX (
                CALCULATETABLE ( ALLSELECTED ( 'Churn Risk' ), ALLSELECTED ( Customer[company_name] ) ),
                'Churn Risk'[mrr_at_risk], [Expected MRR at risk], DESC, Dense
            )
        )""", "0", "Rank by expected MRR loss within the current slicer selection; the call list shows the top 8."),
    ("Leak alerts", "COUNTROWS ( Alerts )", "#,0", "Alerts raised by the revenue leak monitor."),
    ("Driver weight", "SUM ( Drivers[mean_abs_contribution] )", "0.000", "Average absolute contribution to the churn score."),
    # ---------------- Account deep dive
    ("Health score", "AVERAGE ( Health[health_score] )", "0", "Composite health score, 0 to 100."),
    ("Sessions", "SUM ( 'Usage Weekly'[sessions] )", "#,0", "Product sessions."),
    ("Active users", "SUM ( 'Usage Weekly'[weekly_active_users] )", "#,0", "Distinct users active in the week."),
    ("Churn probability", "MAX ( 'Churn Risk'[churn_probability] )", "0%", "Model probability of churning in the next 60 days."),
    ("Account heading", """
        VAR n = SELECTEDVALUE ( Customer[company_name], "Pick one account" )
        VAR sg = SELECTEDVALUE ( 'Churn Risk'[segment] )
        VAR am = SELECTEDVALUE ( 'Churn Risk'[account_manager] )
        RETURN n & IF ( NOT ISBLANK ( sg ), "  ·  " & sg & ", managed by " & am, "  ·  not currently paying" )""", None,
     "Selected account with its segment and account manager."),
    ("Account MRR", "[MRR]", USD, "MRR of the selected account, latest month."),
    ("Ticket recency", """
        IF ( HASONEVALUE ( Tickets[ticket_id] ),
             RANKX ( ALLSELECTED ( Tickets ), Tickets[ticket_id], MAX ( Tickets[ticket_id] ), DESC, Dense ) )""", "0",
     "1 = newest ticket in the current selection; the ticket log shows the newest 6."),
    ("Ledger recency", """
        IF ( HASONEVALUE ( 'MRR Movements'[movement_id] ),
             RANKX ( ALLSELECTED ( 'MRR Movements' ), 'MRR Movements'[date_key], MAX ( 'MRR Movements'[date_key] ), DESC, Dense ) )""",
     "0", "1 = latest MRR change in the current selection; the ledger shows the latest 6."),
    ("Account reasons", """
        VAR d1 = SELECTEDVALUE ( 'Churn Risk'[driver_1] )
        VAR d2 = SELECTEDVALUE ( 'Churn Risk'[driver_2] )
        VAR d3 = SELECTEDVALUE ( 'Churn Risk'[driver_3] )
        RETURN
            IF ( ISBLANK ( d1 ), "No current churn score (account not paying at the end of the data).",
                 "Why the model flags it: " & d1 & IF ( NOT ISBLANK ( d2 ), "; " & d2 ) & IF ( NOT ISBLANK ( d3 ), "; " & d3 ) & "." )""",
     None, "Top drivers of the selected account's churn score, in plain words."),

    ("Insight: accounts", """
        VAR high_loss = CALCULATE ( SUM ( 'Churn Risk'[mrr_at_risk] ), 'Churn Risk'[risk_tier] = "High" )
        RETURN
            [High-risk accounts] & " high-risk accounts, " & FORMAT ( [High-risk MRR] / 1e3, "$#,0" ) & "K MRR, "
                & FORMAT ( high_loss / 1e3, "$#,0" ) & "K expected to churn in 60 days. Flagged: "
                & FORMAT ( [Model lift], "0.0" ) & "x churn rate." """, None, "One-line reading of the accounts page."),
]

CALC_TABLES = {"Metrics": ("{ 1 }", [("Value", "int64", "[Value]", True, None)])}

TYPE_MAP = {"DATE": ("dateTime", "type date", "Short Date"), "TIMESTAMP": ("dateTime", "type datetime", "General Date"),
            "VARCHAR": ("string", "type text", None), "BIGINT": ("int64", "Int64.Type", "0"),
            "INTEGER": ("int64", "Int64.Type", "0"), "HUGEINT": ("int64", "Int64.Type", "0"),
            "DOUBLE": ("double", "type number", "#,0.00"), "FLOAT": ("double", "type number", "#,0.00"),
            "BOOLEAN": ("boolean", "type logical", None)}
COLUMN_FORMATS = {"mrr": USD, "mrr_at_risk": USD, "churn_probability": "0%", "month_start": "mmm yy",
                  "detected_at": "d mmm yyyy", "week_start": "d mmm yy", "mrr_before": USD, "mrr_after": USD,
                  "movement_date": "d mmm yyyy", "created_at": "d mmm yyyy", "resolution_hours": "0.0"}
DEFAULT_ACCOUNT = None    # company opened on the Account page; set in main() from the call list
# Table column widths (px). Set from measured text: powerbi/check_fit.py fails the build if any cell would not fit.
CALL_LIST_WIDTHS = [206, 82, 112, 62, 42, 214, 92]
ALERT_WIDTHS = [90, 130, 580]
LEDGER_WIDTHS = [90, 84, 96, 76, 50, 92, 92]
TICKET_WIDTHS = [90, 64, 116, 60, 116, 46]


def fmt_value(fmt):
    """TMDL needs values containing quotes wrapped in quotes, with inner quotes doubled."""
    return '"' + fmt.replace('"', '""') + '"' if '"' in fmt else fmt


def q(name):
    return f"'{name}'" if any(c in name for c in " .=:'-()") else name


def tmdl_type(duck_type):
    if duck_type.startswith("DECIMAL"):
        return TYPE_MAP["DOUBLE"]
    return TYPE_MAP[duck_type]


def indent(text, tabs):
    lines = text.strip("\n").splitlines()
    pad = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
    return "\n".join("\t" * tabs + l[pad:] for l in lines)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def write_json(path, obj):
    write(path, json.dumps(obj, indent=2, ensure_ascii=False))


def build_model(con):
    d = SM / "definition"
    write_json(SM / "definition.pbism", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
        "version": "4.2", "settings": {}})
    write_json(SM / ".platform", platform("SemanticModel"))
    write(d / "database.tmdl", "database\n\tcompatibilityLevel: 1601\n")
    refs = "\n".join(f"ref table {q(t)}" for t in list(TABLES) + list(CALC_TABLES))
    write(d / "model.tmdl", f"""model Model
\tculture: en-US
\tdefaultPowerBIDataSourceVersion: powerBI_V3
\tdiscourageImplicitMeasures
\tsourceQueryCulture: en-US
\tdataAccessOptions
\t\tlegacyRedirects
\t\treturnErrorValuesAsNull

annotation __PBI_TimeIntelligenceEnabled = 0

annotation PBI_ProTooling = ["TMDL-Extension"]

{refs}
""")
    write(d / "expressions.tmdl",
          f'expression DataFolder = "{DATA_FOLDER}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]\n')

    for table, (source, visible) in TABLES.items():
        cols = con.sql(f"DESCRIBE {EXPORTS[source]}").fetchall()
        out = [f"table {q(table)}", ""]
        for col, ctype, *_ in cols:
            dtype, _, fmt = tmdl_type(ctype)
            fmt = COLUMN_FORMATS.get(col, fmt)
            out.append(f"\tcolumn {q(col)}")
            out.append(f"\t\tdataType: {dtype}")
            if fmt:
                out.append(f"\t\tformatString: {fmt_value(fmt)}")
            if col not in visible:
                out.append("\t\tisHidden")
            if (table, col) in SORT_BY:
                out.append(f"\t\tsortByColumn: {SORT_BY[(table, col)]}")
            out.append("\t\tsummarizeBy: none")
            out.append(f"\t\tsourceColumn: {col}")
            out.append("")
        types = ",\n".join(f'\t\t\t\t\t\t{{"{c}", {tmdl_type(t)[1]}}}' for c, t, *_ in cols)
        out.append(f"\tpartition {q(table)} = m")
        out.append("\t\tmode: import")
        out.append("\t\tsource =")
        out.append(f"""\t\t\t\tlet
\t\t\t\t    Source = Csv.Document(File.Contents(DataFolder & "{source}.csv"), [Delimiter = ",", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),
\t\t\t\t    #"Promoted headers" = Table.PromoteHeaders(Source, [PromoteAllScalars = true]),
\t\t\t\t    #"Typed columns" = Table.TransformColumnTypes(
\t\t\t\t        #"Promoted headers",
\t\t\t\t        {{
{types}
\t\t\t\t        }},
\t\t\t\t        "en-US"
\t\t\t\t    )
\t\t\t\tin
\t\t\t\t    #"Typed columns"
""")
        write(d / "tables" / f"{table}.tmdl", "\n".join(out))

    for table, (dax, cols) in CALC_TABLES.items():
        out = [f"table {q(table)}", ""]
        for name, expr, fmt, desc in MEASURES:
            out.append(f"\t/// {desc}")
            if "\n" in expr.strip():
                out.append(f"\tmeasure {q(name)} = ```")
                out.append(indent(expr, 3))
                out.append("\t\t\t```")
            else:
                out.append(f"\tmeasure {q(name)} = {expr.strip()}")
            if fmt:
                out.append(f"\t\tformatString: {fmt_value(fmt)}")
            out.append("")
        for col, dtype, src, hidden, fmt in cols:
            out.append(f"\tcolumn {q(col)}")
            out.append(f"\t\tdataType: {dtype}")
            if hidden:
                out.append("\t\tisHidden")
            out.append("\t\tsummarizeBy: none")
            out.append(f"\t\tsourceColumn: {src}")
            out.append("")
        out.append(f"\tpartition {q(table)} = calculated")
        out.append("\t\tmode: import")
        out.append(f"\t\tsource = {dax}")
        out.append("")
        write(d / "tables" / f"{table}.tmdl", "\n".join(out))

    rels = []
    for ft, fc, tt, tc in RELATIONSHIPS:
        rid = uuid.uuid5(uuid.NAMESPACE_URL, f"{NAME}:{ft}.{fc}->{tt}.{tc}")
        rels.append(f"relationship {rid}\n\tfromColumn: {q(ft)}.{q(fc)}\n\ttoColumn: {q(tt)}.{q(tc)}\n")
    write(d / "relationships.tmdl", "\n".join(rels))


# ---------------------------------------------------------------- report helpers
def uid(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:20]


def platform(kind):
    return {"$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
            "metadata": {"type": kind, "displayName": NAME},
            "config": {"version": "2.0", "logicalId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{NAME}-{kind}"))}}


def lit(v):
    return {"expr": {"Literal": {"Value": v}}}


def s(text):
    return lit("'" + text.replace("'", "''") + "'")


def num(v, suffix="D"):
    return lit(f"{v}{suffix}")


def color(hex_):
    return {"solid": {"color": s(hex_)}}


def measure(name, entity="Metrics"):
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def column(entity, name):
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": name}}


def measure_color(name):
    return {"solid": {"color": {"expr": measure(name)}}}


def proj(field, display=None):
    kind = "Measure" if "Measure" in field else "Column"
    ent = field[kind]["Expression"]["SourceRef"]["Entity"]
    prop = field[kind]["Property"]
    p = {"field": field, "queryRef": f"{ent}.{prop}", "nativeQueryRef": prop}
    if kind == "Column":
        p["active"] = True
    if display:
        p["displayName"] = display
    return p


def container(title=None, bg=None):
    c = {"title": [{"properties": {"show": lit("true" if title else "false")}}],
         "background": [{"properties": {"show": lit("true" if bg else "false")}}],
         "border": [{"properties": {"show": lit("false")}}],
         "dropShadow": [{"properties": {"show": lit("false")}}],
         "visualHeader": [{"properties": {"show": lit("false")}}]}
    if title:
        c["title"][0]["properties"].update({"text": s(title), "fontFamily": s(SANS), "fontSize": num(11),
                                            "fontColor": color(MUTED), "bold": lit("false")})
    if bg:
        c["background"][0]["properties"].update({"color": color(bg), "transparency": num(0)})
    return c


def categorical_filter(entity, col, values, name):
    return {"name": name, "field": column(entity, col), "type": "Categorical",
            "filter": {"Version": 2, "From": [{"Name": "f", "Entity": entity, "Type": 0}],
                       "Where": [{"Condition": {"In": {
                           "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "f"}}, "Property": col}}],
                           "Values": [[{"Literal": {"Value": v}}] for v in values]}}}]},
            "howCreated": "User"}


class Page:
    def __init__(self, key, display):
        self.key, self.display, self.visuals, self.interactions = key, display, [], []

    def add(self, vid, x, y, w, h, visual, filters=None):
        v = {"$schema": f"{SCHEMA}/visualContainer/2.1.0/schema.json",
             "name": uid(self.key, vid),
             "position": {"x": x, "y": y, "z": 1000 * (len(self.visuals) + 1),
                          "height": h, "width": w, "tabOrder": 1000 * (len(self.visuals) + 1)},
             "visual": visual}
        if filters:
            v["filterConfig"] = {"filters": filters}
        v["_folder"] = vid
        self.visuals.append(v)

    def no_filter(self, source, target):
        self.interactions.append({"source": uid(self.key, source), "target": uid(self.key, target), "type": "NoFilter"})


def textbox(paragraphs):
    return {"visualType": "textbox", "objects": {"general": [{"properties": {"paragraphs": paragraphs}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def run(text, font=SANS, size=10, col=TEXT, bold=False):
    style = {"fontFamily": font, "fontSize": f"{size}pt", "color": col}
    if bold:
        style["fontWeight"] = "bold"
    return {"value": text, "textStyle": style}


def kpi_cards(fields):
    return {"visualType": "cardVisual",
            "query": {"queryState": {"Data": {"projections": [proj(measure(m), d) for m, d in fields]}}},
            "objects": {
                "layout": [{"properties": {"orientation": num(0), "columnCount": num(len(fields), "L"),
                                           "style": s("Cards"), "alignment": s("left")}}],
                "value": [{"properties": {"fontColor": color(TEXT), "fontSize": num(22), "fontFamily": s(SERIF),
                                          "labelDisplayUnits": num(1)},
                           "selector": {"id": "default"}}],
                "label": [{"properties": {"fontColor": color(MUTED), "fontSize": num(10), "fontFamily": s(SANS),
                                          "position": s("aboveValue")}, "selector": {"id": "default"}}],
                "fillCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "outline": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "accentBar": [{"properties": {"show": lit("true"), "position": s("Left"), "color": color(RULE),
                                              "width": num(2)}, "selector": {"id": "default"}}],
                "shadowCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "padding": [{"properties": {"paddingSelection": s("Narrow")}, "selector": {"id": "default"}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def insight(measure_name):
    return {"visualType": "cardVisual",
            "query": {"queryState": {"Data": {"projections": [proj(measure(measure_name))]}}},
            "objects": {
                "layout": [{"properties": {"orientation": num(0), "columnCount": num(1, "L"), "alignment": s("left")}}],
                "value": [{"properties": {"fontColor": color(MUTED), "fontSize": num(11), "fontFamily": s(SANS),
                                          "horizontalAlignment": s("left")}, "selector": {"id": "default"}}],
                "label": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "fillCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "outline": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "accentBar": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "shadowCustom": [{"properties": {"show": lit("false")}, "selector": {"id": "default"}}],
                "padding": [{"properties": {"paddingSelection": s("Narrow")}, "selector": {"id": "default"}}]},
            "visualContainerObjects": container(), "drillFilterOtherVisuals": True}


def axis_objects():
    return {"categoryAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                             "gridlineShow": lit("false")}}],
            "valueAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                          "gridlineColor": color(RULE), "gridlineThickness": num(1, "L")}}],
            "legend": [{"properties": {"show": lit("true"), "position": s("Top"), "labelColor": color(TEXT),
                                       "fontSize": num(9), "showTitle": lit("false")}}]}


def measure_colors(mapping):
    return [{"properties": {"fill": color(hex_)}, "selector": {"metadata": f"Metrics.{m}"}} for m, hex_ in mapping.items()]


def chart(vtype, category, values, title, series=None, extra=None, sort=None):
    qs = {"Category": {"projections": [proj(category)]},
          "Y": {"projections": [proj(measure(v)) for v in values]}}
    if series:
        qs["Series"] = {"projections": [proj(series)]}
    v = {"visualType": vtype, "query": {"queryState": qs}, "objects": axis_objects(),
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}
    if extra:
        v["objects"].update(extra)
    return v


def bars(category, value, title, fill=INK, sort=None):
    return chart("barChart", category, [value], title, sort=sort, extra={
        "dataPoint": [{"properties": {"fill": color(fill)}}],
        "categoryAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                         "maxMarginFactor": num(45, "L")}}],
        "valueAxis": [{"properties": {"show": lit("false"), "showAxisTitle": lit("false"), "start": num(0)}}],
        "labels": [{"properties": {"show": lit("true"), "color": color(MUTED), "fontSize": num(9)}}],
        "legend": [{"properties": {"show": lit("false")}}]})


def slicer(entity, col, title, mode="Dropdown", sync=None, default_text=None):
    v = {"visualType": "slicer",
         "query": {"queryState": {"Values": {"projections": [proj(column(entity, col))]}}},
         "objects": {"data": [{"properties": {"mode": s(mode)}}],
                     "header": [{"properties": {"show": lit("true"), "text": s(title), "fontColor": color(MUTED),
                                                "fontFamily": s(SANS), "textSize": num(10)}}],
                     "items": [{"properties": {"fontColor": color(TEXT), "fontFamily": s(SANS), "textSize": num(11)}}]},
         "visualContainerObjects": container(), "drillFilterOtherVisuals": True}
    if default_text is not None:
        # open with one value selected (single-select), the same way Desktop saves a user's pick
        v["objects"]["selection"] = [{"properties": {"strictSingleSelect": lit("true")}}]
        v["objects"]["general"] = [{"properties": {"filter": {"filter": {
            "Version": 2, "From": [{"Name": "p", "Entity": entity, "Type": 0}],
            "Where": [{"Condition": {"Comparison": {
                "ComparisonKind": 0,
                "Left": {"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": col}},
                "Right": {"Literal": {"Value": "'" + default_text.replace("'", "''") + "'"}}}}}]}}}}]
    if sync:
        v["syncGroup"] = {"groupName": sync, "fieldChanges": True, "filterChanges": True}
    return v


def matrix(rows, cols, value, back, title, cell_px=None, font=9, header_font=8, padding=2, fore=None, fore_measure=None):
    """A grid of coloured cells: rows x columns, cell colour from a measure that returns a hex code.
    fore_measure (optional) returns the text colour per cell, so dark cells get light text."""
    v = {"visualType": "pivotTable",
         "query": {"queryState": {"Rows": {"projections": [proj(rows, " ")]},
                                  "Columns": {"projections": [proj(cols)]},
                                  "Values": {"projections": [proj(measure(value))]}}},
         "objects": {
             "values": [{"properties": {"fontColor": color(fore or TEXT), "fontSize": num(font), "fontFamily": s(SANS),
                                        "wordWrap": lit("false"),
                                        "backColorPrimary": color(PAPER), "backColorSecondary": color(PAPER)}},
                        {"properties": {"backColor": measure_color(back),
                                        **({"fontColor": measure_color(fore_measure)} if fore_measure else {})},
                         "selector": {"data": [{"dataViewWildcard": {"matchingOption": 1}}], "metadata": f"Metrics.{value}"}}],
             "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(header_font), "fontFamily": s(SANS),
                                               "backColor": color(PAPER), "outline": s("None"), "wordWrap": lit("false"),
                                               "alignment": s("Center"), "autoSizeColumnWidth": lit("false")}}],
             "rowHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(9), "fontFamily": s(SANS),
                                            "backColor": color(PAPER), "outline": s("None"), "showExpandCollapseButtons": lit("false")}}],
             "grid": [{"properties": {"gridVertical": lit("true"), "gridVerticalColor": color(PAPER), "gridVerticalWeight": num(2, "L"),
                                      "gridHorizontal": lit("true"), "gridHorizontalColor": color(PAPER), "gridHorizontalWeight": num(2, "L"),
                                      "outlineColor": color(PAPER), "rowPadding": num(padding, "L"), "textSize": num(font)}}],
             "subTotals": [{"properties": {"rowSubtotals": lit("false"), "columnSubtotals": lit("false")}}]},
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if cell_px:
        v["objects"]["columnWidth"] = [{"properties": {"value": num(cell_px)}, "selector": {"metadata": f"Metrics.{value}"}}]
    return v


def measure_filter(measure_name, kind, value, name):
    """Visual-level filter on a measure. kind: ComparisonKind (4 = less than or equal)."""
    return {"name": name, "field": measure(measure_name), "type": "Advanced",
            "filter": {"Version": 2, "From": [{"Name": "m", "Entity": "Metrics", "Type": 0}],
                       "Where": [{"Condition": {"Comparison": {
                           "ComparisonKind": kind,
                           "Left": {"Measure": {"Expression": {"SourceRef": {"Source": "m"}}, "Property": measure_name}},
                           "Right": {"Literal": {"Value": value}}}}}]},
            "howCreated": "User"}


def table(columns, title, sort=None, widths=None):
    """Single-line rows: column widths are set from measured text (powerbi/check_fit.py), so nothing wraps or truncates."""
    v = {"visualType": "tableEx",
         "query": {"queryState": {"Values": {"projections": [proj(f, d) for f, d in columns]}}},
         "objects": {
             "values": [{"properties": {"fontColor": color(TEXT), "fontSize": num(9), "fontFamily": s(SANS),
                                        "backColorPrimary": color(PAPER), "backColorSecondary": color(PAPER),
                                        "wordWrap": lit("false")}}],
             "columnHeaders": [{"properties": {"fontColor": color(MUTED), "fontSize": num(9), "fontFamily": s(SANS),
                                               "backColor": color(PAPER), "outline": s("BottomOnly"),
                                               "autoSizeColumnWidth": lit("false")}}],
             "grid": [{"properties": {"gridVertical": lit("false"), "gridHorizontal": lit("true"),
                                      "gridHorizontalColor": color(RULE), "outlineColor": color(RULE), "rowPadding": num(3, "L")}}],
             "total": [{"properties": {"totals": lit("false")}}]},
         "visualContainerObjects": container(title), "drillFilterOtherVisuals": True}
    if widths:
        v["objects"]["columnWidth"] = [
            {"properties": {"value": num(w)}, "selector": {"metadata": proj(f)["queryRef"]}}
            for (f, _), w in zip(columns, widths)]
    if sort:
        field, direction = sort
        v["query"]["sortDefinition"] = {"sort": [{"field": field, "direction": direction}], "isDefaultSort": False}
    return v


# ---------------------------------------------------------------- report pages
X0, W = 24, 1232          # content column
HALF = 604                # two columns with a 24 px gutter


def header(page):
    page.add("title", X0, 10, 460, 64, textbox([
        {"textRuns": [run("Revenue leak", SERIF, 18, TEXT)]},
        {"textRuns": [run("Synthetic B2B SaaS, about 1,100 accounts. Oct 2024 to Sep 2026", SANS, 8, MUTED)]}]))

    def state(sel, col, bar):
        return {"text": {"properties": {"fontFamily": s(SERIF), "fontSize": num(11), "fontColor": color(col)},
                         "selector": {"id": sel}},
                "fill": {"properties": {"show": lit("true"), "fillColor": color(PAPER), "transparency": num(0)},
                         "selector": {"id": sel}},
                "accentBar": {"properties": {"show": lit(bar), "position": s("Bottom"), "color": color(INK),
                                             "width": num(2, "L")}, "selector": {"id": sel}}}
    states = [state("default", MUTED, "false"), state("hover", TEXT, "false"), state("press", TEXT, "false"),
              state("selected", INK, "true")]
    nav = {k: [st[k] for st in states] for k in ("text", "fill", "accentBar")}
    nav["outline"] = [{"properties": {"show": lit("false")}}]
    nav["layout"] = [{"properties": {"orientation": num(0)}}]
    page.add("nav", 716, 14, 540, 48, {"visualType": "pageNavigator", "objects": nav,
             "visualContainerObjects": container(), "drillFilterOtherVisuals": True})


def filters_row(page, insight_measure):
    page.add("period", X0, 80, 180, 56, slicer("Date", "fiscal_year", "Fiscal year (Feb-Jan)", sync="period"))
    page.add("segment", X0 + 196, 80, 180, 56, slicer("Customer", "segment", "Segment", sync="segment"))
    page.add("insight", X0 + 400, 84, W - 400, 48, insight(insight_measure))


def build_pages():
    pages = []
    month = column("Date", "month_start")

    # ------------------------------------------------ Revenue
    p = Page("revenue", "Revenue")
    header(p)
    filters_row(p, "Insight: revenue")
    p.add("kpis", X0, 148, W, 84, kpi_cards([("MRR ($M)", "MRR"), ("ARR ($M)", "ARR"), ("MRR growth (12 m)", "MRR growth, 12 months"),
                                             ("NRR (12 m)", "NRR, trailing 12 m"), ("GRR (monthly)", "Gross retention, monthly"),
                                             ("Logo churn (monthly)", "Logo churn, monthly")]))
    p.add("mrrTrend", X0, 248, HALF, 262, chart(
        "areaChart", month, ["MRR"], "MRR at month end",
        extra={"dataPoint": measure_colors({"MRR": INK}), "legend": [{"properties": {"show": lit("false")}}]}))
    p.add("movements", X0 + HALF + 24, 248, HALF, 262, chart(
        "columnChart", month, ["New", "Expansion", "Reactivation", "Contraction", "Churn"],
        "MRR movements by month: what was added and what leaked",
        extra={"dataPoint": measure_colors({"New": INK, "Expansion": TEAL, "Reactivation": "#9aa6c8",
                                            "Contraction": OCHRE, "Churn": RUST})}))
    p.add("retention", X0, 526, HALF, 180, chart(
        "lineChart", month, ["NRR (monthly)", "GRR (monthly)"], "Net and gross revenue retention, month over month",
        extra={"dataPoint": measure_colors({"NRR (monthly)": INK, "GRR (monthly)": RUST})}))
    p.add("lostBySegment", X0 + HALF + 24, 526, HALF, 180, bars(
        column("Customer", "segment"), "Lost MRR", "MRR lost to churn and downgrades, by segment", fill=RUST,
        sort=(measure("Lost MRR"), "Descending")))
    pages.append(p)

    # ------------------------------------------------ Cohorts and adoption
    p = Page("cohorts", "Cohorts")
    header(p)
    filters_row(p, "Insight: cohorts")
    p.add("kpis", X0, 148, W, 76, kpi_cards([("Month-6 logo retention", "Still paying at month 6"),
                                             ("Month-12 revenue retention", "Revenue kept, month 12"),
                                             ("Weekly active users", "Weekly active users"),
                                             ("Sessions per account", "Weekly sessions/account"),
                                             ("Average CSAT", "Average CSAT"),
                                             ("Resolution SLA breached", "Tickets past SLA")]))
    # month 0 is 100% by definition, so the grid starts at month 1
    p.add("heatmap", X0, 236, 868, 480, matrix(
        column("Cohorts", "cohort_label"), column("Cohorts", "months_since_signup"), "Cohort cell", "Cohort colour",
        "Customers still paying (%) by signup cohort and months since signup; all segments, darker = more lost",
        cell_px=34, font=8, header_font=8, padding=0, fore_measure="Cohort text colour"),
        filters=[categorical_filter("Cohorts", "months_since_signup", [f"{m}L" for m in range(1, 24)], "afterSignup")])
    weekly = column("Date", "week_start")
    complete = [categorical_filter("Usage Weekly", "is_complete_week", ["true"], "completeWeeks")]
    p.add("reports", X0 + 892, 236, 340, 232, chart(
        "lineChart", weekly, ["Reports per account", "Dashboards per account"], "Feature use per account, weekly, from 2025",
        extra={"dataPoint": measure_colors({"Reports per account": RUST, "Dashboards per account": INK})}),
        filters=complete + [categorical_filter("Date", "year", ["2025L", "2026L"], "from2025")])
    p.add("healthMix", X0 + 892, 484, 340, 232, bars(
        column("Health", "health_band"), "Health accounts", "Accounts by health band, latest week", fill=INK,
        sort=(column("Health", "health_band"), "Ascending")))
    for v in ("heatmap",):
        p.no_filter("period", v)
        p.no_filter("segment", v)
    pages.append(p)

    # ------------------------------------------------ Accounts (customer success)
    p = Page("accounts", "Accounts")
    header(p)
    p.add("segment", X0, 80, 180, 56, slicer("Customer", "segment", "Segment", sync="segment"))
    p.add("am", X0 + 196, 80, 180, 56, slicer("Customer", "account_manager", "Account manager"))
    p.add("insight", X0 + 400, 84, W - 400, 48, insight("Insight: accounts"))
    p.add("kpis", X0, 148, W, 84, kpi_cards([("High-risk accounts", "High-risk accounts"), ("High-risk MRR ($K)", "High-risk MRR"),
                                             ("Expected MRR at risk ($K)", "Expected loss, whole book"),
                                             ("At-risk health accounts", "At Risk on health score"),
                                             ("Model lift", "Model lift, top 10%"), ("Leak alerts", "Leak alerts raised")]))
    p.add("actionMatrix", X0, 248, 380, 170, matrix(
        column("Churn Risk", "risk_tier"), column("Churn Risk", "health_band"), "Matrix cell", "Matrix colour",
        "Model risk x health: MRR · accounts", cell_px=104, font=9, header_font=9, padding=6,
        fore_measure="Matrix text colour"))
    p.add("riskByAm", X0, 432, 380, 274, bars(
        column("Churn Risk", "account_manager"), "Expected MRR at risk", "Expected MRR at risk by account manager",
        fill=RUST, sort=(measure("Expected MRR at risk"), "Descending")))
    p.add("actionList", X0 + 404, 248, 828, 272, table(
        [(column("Customer", "company_name"), "Account"), (column("Churn Risk", "segment"), "Segment"),
         (column("Churn Risk", "account_manager"), "AM"), (column("Churn Risk", "mrr"), "MRR"),
         (column("Churn Risk", "churn_probability"), "Risk"), (column("Churn Risk", "driver_1"), "Main reason"),
         (column("Churn Risk", "mrr_at_risk"), "Expected loss")],
        "Call list: top 8 high-risk accounts by expected loss",
        sort=(column("Churn Risk", "mrr_at_risk"), "Descending"), widths=CALL_LIST_WIDTHS),
        filters=[categorical_filter("Churn Risk", "risk_tier", ["'High'"], "highRisk"),
                 measure_filter("Call rank", 4, "8L", "top8")])
    p.add("alerts", X0 + 404, 532, 828, 174, table(
        [(column("Alerts", "detected_at"), "Detected"), (column("Alerts", "detector"), "Detector"),
         (column("Alerts", "detail"), "What the monitor saw")],
        "Revenue leak monitor: latest alert for each of the 4 most recent issues",
        sort=(column("Alerts", "detected_at"), "Descending"), widths=ALERT_WIDTHS),
        filters=[categorical_filter("Alerts", "recency_rank", ["1L", "2L", "3L", "4L"], "newest4")])
    for src in ("segment", "am"):
        p.no_filter(src, "alerts")
    pages.append(p)

    # ------------------------------------------------ Account deep dive
    p = Page("account", "Account")
    header(p)
    p.add("account", X0, 80, 320, 56, slicer("Customer", "company_name", "Account (type to search)",
                                             default_text=DEFAULT_ACCOUNT))
    heading = insight("Account heading")
    heading["objects"]["value"][0]["properties"].update({"fontColor": color(TEXT), "fontSize": num(16), "fontFamily": s(SERIF)})
    p.add("heading", X0 + 344, 84, W - 344, 48, heading)
    p.add("kpis", X0, 148, W, 84, kpi_cards([("Account MRR", "MRR, latest month"), ("Churn probability", "60-day churn probability"),
                                             ("Health score", "Health score, average"), ("Tickets", "Tickets"),
                                             ("Average CSAT", "Average CSAT"), ("Lost MRR", "MRR lost")]))
    p.add("reasons", X0, 234, W, 48, insight("Account reasons"))
    p.add("mrrHistory", X0, 284, 400, 206, chart(
        "areaChart", month, ["MRR"], "MRR at month end",
        extra={"dataPoint": measure_colors({"MRR": INK}), "legend": [{"properties": {"show": lit("false")}}]}))
    weekly = column("Date", "week_start")
    p.add("usage", X0 + 416, 284, 400, 206, chart(
        "lineChart", weekly, ["Sessions", "Active users"], "Weekly sessions and active users",
        extra={"dataPoint": measure_colors({"Sessions": INK, "Active users": TEAL})}),
        filters=[categorical_filter("Usage Weekly", "is_complete_week", ["true"], "completeWeeks")])
    p.add("health", X0 + 832, 284, 400, 206, chart(
        "lineChart", weekly, ["Health score"], "Health score by week (below 50 = At Risk)",
        extra={"dataPoint": measure_colors({"Health score": RUST}), "legend": [{"properties": {"show": lit("false")}}],
               "valueAxis": [{"properties": {"labelColor": color(MUTED), "fontSize": num(9), "showAxisTitle": lit("false"),
                                             "gridlineColor": color(RULE), "start": num(0), "end": num(100)}}]}))
    p.add("ledger", X0, 502, 604, 204, table(
        [(column("MRR Movements", "movement_date"), "Date"), (column("MRR Movements", "movement_type"), "Movement"),
         (column("MRR Movements", "change_reason"), "Reason"), (column("MRR Movements", "plan_id"), "Plan"),
         (column("MRR Movements", "seats"), "Seats"), (column("MRR Movements", "mrr_before"), "MRR before"),
         (column("MRR Movements", "mrr_after"), "MRR after")],
        "Billing ledger: 6 latest MRR changes", sort=(column("MRR Movements", "movement_date"), "Descending"),
        widths=LEDGER_WIDTHS), filters=[measure_filter("Ledger recency", 4, "6L", "latest6")])
    p.add("tickets", X0 + 628, 502, 604, 204, table(
        [(column("Tickets", "created_at"), "Opened"), (column("Tickets", "priority"), "Priority"),
         (column("Tickets", "category"), "Category"), (column("Tickets", "status"), "Status"),
         (column("Tickets", "resolution_hours"), "Hours to resolve"), (column("Tickets", "csat_score"), "CSAT")],
        "Support tickets: 6 newest", sort=(column("Tickets", "created_at"), "Descending"),
        widths=TICKET_WIDTHS), filters=[measure_filter("Ticket recency", 4, "6L", "newest6")])
    pages.append(p)
    return pages


def theme():
    return {
        "name": "Editorial ledger",
        "dataColors": [INK, OCHRE, TEAL, RUST, PLUM, "#5e6676", "#9aa6c8", "#c9b99a"],
        "background": PAPER, "foreground": TEXT, "tableAccent": INK,
        "good": TEAL, "neutral": OCHRE, "bad": RUST,
        "textClasses": {
            "callout": {"fontFace": SERIF, "fontSize": 22, "color": TEXT},
            "title": {"fontFace": SANS, "fontSize": 11, "color": MUTED},
            "header": {"fontFace": SANS, "fontSize": 11, "color": TEXT},
            "label": {"fontFace": SANS, "fontSize": 10, "color": MUTED}},
        "visualStyles": {
            "page": {"*": {"background": [{"color": {"solid": {"color": PAPER}}, "transparency": 0}],
                           "outspace": [{"color": {"solid": {"color": PAPER}}}]}},
            "*": {"*": {"background": [{"show": False}], "border": [{"show": False}],
                        "dropShadow": [{"show": False}]}}},
    }


def build_report(active_page=None):
    d = RP / "definition"
    write_json(RP / ".platform", platform("Report"))
    write_json(RP / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": f"../{NAME}.SemanticModel"}}})
    write_json(d / "version.json", {"$schema": f"{SCHEMA}/versionMetadata/1.0.0/schema.json", "version": "2.0.0"})
    write_json(RP / "StaticResources" / "RegisteredResources" / "theme.json", theme())
    write_json(d / "report.json", {
        "$schema": f"{SCHEMA}/report/3.0.0/schema.json",
        "themeCollection": {
            "baseTheme": {"name": "CY24SU10", "reportVersionAtImport": {"visual": "1.8.97", "report": "2.0.97", "page": "1.3.97"},
                          "type": "SharedResources"},
            "customTheme": {"name": "theme.json", "reportVersionAtImport": {"visual": "1.8.100", "report": "2.0.100", "page": "1.3.100"},
                            "type": "RegisteredResources"}},
        "resourcePackages": [
            {"name": "SharedResources", "type": "SharedResources",
             "items": [{"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": "BaseTheme"}]},
            {"name": "RegisteredResources", "type": "RegisteredResources",
             "items": [{"name": "theme.json", "path": "theme.json", "type": "CustomTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "defaultDrillFilterOtherVisuals": True,
                     "useEnhancedTooltips": True, "hideVisualContainerHeader": True}})
    pages = build_pages()
    write_json(d / "pages" / "pages.json", {
        "$schema": f"{SCHEMA}/pagesMetadata/1.0.0/schema.json",
        "pageOrder": [p.key for p in pages], "activePageName": active_page or pages[0].key})
    for p in pages:
        page = {"$schema": f"{SCHEMA}/page/1.4.0/schema.json", "name": p.key, "displayName": p.display,
                "displayOption": "FitToPage", "height": 720, "width": 1280}
        if p.interactions:
            page["visualInteractions"] = p.interactions
        write_json(d / "pages" / p.key / "page.json", page)
        for v in p.visuals:
            folder = v.pop("_folder")
            write_json(d / "pages" / p.key / "visuals" / folder / "visual.json", v)


def main():
    global DATA_FOLDER
    parser = argparse.ArgumentParser(description="Generate the Power BI project.")
    parser.add_argument("--portable", action="store_true",
                        help="use the neutral path C:\\saas-revenue-leak\\exports instead of this checkout's path")
    parser.add_argument("--db", default=str(ROOT / "warehouse" / "revenue.duckdb"))
    args = parser.parse_args()
    if args.portable:
        DATA_FOLDER = PORTABLE_DATA_FOLDER
    for path in (SM, RP):
        if path.exists():
            shutil.rmtree(path)
    con = duckdb.connect(args.db, read_only=True)
    global DEFAULT_ACCOUNT
    DEFAULT_ACCOUNT = con.sql("""
        select c.company_name from ml.churn_risk_scores r join core.dim_customers c using (customer_sk)
        order by r.mrr_at_risk desc limit 1""").fetchone()[0]
    build_model(con)
    build_report()
    write_json(OUT / f"{NAME}.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": f"{NAME}.Report"}}], "settings": {"enableAutoRecovery": True}})
    print(f"wrote {OUT / (NAME + '.pbip')}")


if __name__ == "__main__":
    main()
