"""Export the presentation layer to CSV for Power BI.

One file per table in exports/. powerbi/build_pbip.py reads the same queries
to type the semantic model, so the two cannot drift apart.

    python scripts/export_marts.py --db warehouse/revenue.duckdb
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

EXPORTS = {
    "dim_date": "(select * from core.dim_date)",
    "dim_customers": "(select * from core.dim_customers)",
    "fact_mrr_movements": "(select * from core.fact_mrr_movements)",
    "fact_product_usage_weekly": """(
        select *,
               week_start >= (select min(usage_date) from core.fact_product_usage)
               and week_start + interval 6 day <= (select max(usage_date) from core.fact_product_usage) as is_complete_week
        from core.fact_product_usage_weekly)""",
    "fact_support_tickets": "(select * exclude (tags) from core.fact_support_tickets)",
    "mrr_waterfall_monthly": "(select * from metrics.mrr_waterfall_monthly)",
    "cohort_retention": "(select * from metrics.cohort_retention)",
    "customer_health_weekly": """(
        select *, case health_band when 'At Risk' then 1 when 'Watch' then 2 else 3 end as health_band_order
        from metrics.customer_health_weekly)""",
    "anomaly_alerts": """(
        -- recency_rank ranks only the latest alert of each issue (detector + what it is about), so a
        -- monthly repeat of the same finding does not crowd out the others
        with latest as (
            select *, row_number() over (partition by detector, dimension_value order by detected_at desc) = 1 as is_latest
            from metrics.anomaly_alerts
        )
        select * exclude (is_latest),
               cast(case when is_latest then row_number() over (partition by is_latest order by detected_at desc, alert_id) end
                    as integer) as recency_rank
        from latest)""",
    "churn_risk_scores": """(
        select r.*,
               coalesce(h.health_band, 'Watch') as health_band,
               case r.risk_tier when 'High' then 1 when 'Medium' then 2 else 3 end as risk_tier_order,
               case coalesce(h.health_band, 'Watch') when 'At Risk' then 1 when 'Watch' then 2 else 3 end as health_band_order
        from ml.churn_risk_scores as r
        left join metrics.customer_health_weekly as h
            on h.account_id = r.account_id and h.snapshot_date = r.snapshot_date)""",
    "feature_importance": """(
        select feature, replace(replace(feature, '_', ' '), '30d', '30 days') as feature_label, mean_abs_contribution
        from ml.feature_importance)""",
    "model_metrics": "(select * from ml.model_metrics)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="warehouse/revenue.duckdb")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "exports"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.parquet"):
        old.unlink()
    con = duckdb.connect(args.db, read_only=True)
    con.execute("set threads = 1")
    for name, query in EXPORTS.items():
        path = (out / f"{name}.csv").as_posix()
        # order by every column: DuckDB writes in parallel, so without it row order can change between runs
        con.execute(f"copy (select * from {query} order by all) to '{path}' (header, delimiter ',')")
        n = con.sql(f"select count(*) from {query}").fetchone()[0]
        print(f"  {name:<28} {n:>9,} rows")
    con.close()


if __name__ == "__main__":
    main()
