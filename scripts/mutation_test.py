"""Mutation test: prove the dbt tests catch real breakage.

Each case copies the built warehouse, breaks it in one specific way (a
duplicate ledger row, an overlapping SCD2 version, a waterfall that no longer
balances, ...), runs only the tests that should notice, and passes if at least
one of them fails. A test suite that stays green on broken data proves nothing.

    python scripts/mutation_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "warehouse" / "revenue.duckdb"
COPY = ROOT / "warehouse" / "mutation.duckdb"
DBT = str(Path(sys.executable).parent / ("dbt.exe" if os.name == "nt" else "dbt"))

# (what breaks, SQL that breaks it, dbt selector for the tests that should catch it)
MUTATIONS = [
    ("duplicate row in the MRR ledger",
     "insert into core.fact_mrr_movements select * from core.fact_mrr_movements limit 1",
     "fact_mrr_movements"),
    ("ledger delta drifts by one dollar",
     "update core.fact_mrr_movements set mrr_delta = mrr_delta + 1 where movement_id = (select min(movement_id) from core.fact_mrr_movements)",
     "assert_mrr_ledger_reconciles"),
    ("negative MRR after a movement",
     "update core.fact_mrr_movements set mrr_after = -10 where movement_id = (select max(movement_id) from core.fact_mrr_movements)",
     "fact_mrr_movements"),
    ("unknown movement type",
     "update core.fact_mrr_movements set movement_type = 'Upsell' where movement_id = (select min(movement_id) from core.fact_mrr_movements)",
     "fact_mrr_movements"),
    ("ticket points at a customer that does not exist",
     "update core.fact_support_tickets set customer_sk = 999999 where ticket_id = (select min(ticket_id) from core.fact_support_tickets)",
     "fact_support_tickets"),
    ("unknown-member row deleted from the customer dimension",
     "delete from core.dim_customers where customer_sk = -1",
     "fact_support_tickets"),
    ("SCD2 versions overlap",
     "update core.dim_customers set valid_to = valid_to + interval 5 day where not is_current and customer_sk = (select min(customer_sk) from core.dim_customers where not is_current and customer_sk > 0)",
     "dim_customers"),
    ("account has two current versions",
     "update core.dim_customers set is_current = true where customer_sk = (select min(customer_sk) from core.dim_customers where not is_current and customer_sk > 0)",
     "dim_customers"),
    ("CSAT outside 1-5 reaches the fact table",
     "update core.fact_support_tickets set csat_score = 7 where ticket_id = (select max(ticket_id) from core.fact_support_tickets)",
     "fact_support_tickets"),
    ("ticket loses its created date",
     "update core.fact_support_tickets set created_date_key = null where ticket_id = (select min(ticket_id) from core.fact_support_tickets)",
     "fact_support_tickets"),
    ("waterfall bridge no longer balances",
     "update metrics.revenue_retention_monthly set churned_mrr = churned_mrr + 100 where month_start = date '2026-03-01'",
     "assert_waterfall_bridge_balances"),
    ("gross revenue retention above 100%",
     "update metrics.revenue_retention_monthly set grr_mom = 1.2 where month_start = date '2026-03-01'",
     "revenue_retention_monthly"),
    ("health score above 100",
     "update metrics.customer_health_weekly set health_score = 120 where health_id = (select min(health_id) from metrics.customer_health_weekly)",
     "customer_health_weekly"),
    ("alert from a detector nobody built",
     "update metrics.anomaly_alerts set detector = 'gut_feeling' where alert_id = (select min(alert_id) from metrics.anomaly_alerts)",
     "anomaly_alerts"),
]


def failures(selector: str) -> tuple[int, str]:
    env = {**os.environ, "DUCKDB_PATH": str(COPY), "RAW_DIR": (ROOT / "data" / "raw").as_posix(),
           "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    subprocess.run([DBT, "test", "--select", selector, "--profiles-dir", ".", "--quiet"],
                   cwd=ROOT / "dbt", env=env, capture_output=True, text=True)
    results = json.loads((ROOT / "dbt" / "target" / "run_results.json").read_text())["results"]
    failed = [r["unique_id"].split(".")[-2 if r["unique_id"].count(".") > 2 else -1] for r in results
              if r["status"] in ("fail", "error")]
    return len(failed), ", ".join(failed[:2])


def main():
    caught = 0
    shutil.copy(DB, COPY)
    n, _ = failures(" ".join(sorted({m[2] for m in MUTATIONS})))
    if n:
        sys.exit(f"baseline is not clean: {n} tests fail before any mutation")
    print(f"baseline: all selected tests pass on the unbroken warehouse")
    for what, sql, selector in MUTATIONS:
        shutil.copy(DB, COPY)
        con = duckdb.connect(str(COPY))
        con.execute(sql)
        con.close()
        n, which = failures(selector)
        caught += n > 0
        print(f"  {'caught' if n else 'MISSED'}  {what:58s} {which}")
    COPY.unlink(missing_ok=True)
    print(f"{caught} of {len(MUTATIONS)} mutations caught")
    sys.exit(0 if caught == len(MUTATIONS) else 1)


if __name__ == "__main__":
    main()
