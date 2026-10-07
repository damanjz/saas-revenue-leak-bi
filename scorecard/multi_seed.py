"""Re-run the whole pipeline on fresh random worlds and collect the scorecards.

Detector thresholds were revised once after the seed-42 run. These seeds were
never inspected before scoring, so they are the out-of-sample check.

    python scorecard/multi_seed.py --label v2 --seeds 7 2024 31337
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--seeds", type=int, nargs="+", default=[7, 2024, 31337])
    args = ap.parse_args()

    rows = []
    for seed in args.seeds:
        print(f"\n##### seed {seed}", flush=True)
        r = subprocess.run([sys.executable, "pipeline.py", "--seed", str(seed)], cwd=ROOT,
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(r.stdout[-3000:] + r.stderr[-3000:])
        con = duckdb.connect(str(ROOT / "warehouse" / "revenue.duckdb"), read_only=True)
        inc = con.sql("select * from scorecard.incidents").df()
        fa = con.sql("select * from scorecard.false_alarms").df()
        ew = con.sql("select * from scorecard.early_warning where true_cause = 'ALL'").df().iloc[0]
        m = con.sql("select * from ml.model_metrics where selected").df().iloc[0]
        con.close()
        row = {"seed": seed}
        for x in inc.itertuples():
            row[x.incident] = "missed" if not x.detected else f"{int(x.days_to_detect)}d"
        row["false_alarms"] = int(fa["related_incident"].isna().sum())
        row["secondary"] = int(fa["related_incident"].notna().sum())
        row["model"] = m["model"]
        row["roc_auc"] = round(m["roc_auc"], 3)
        row["lift_top10"] = round(m["lift_top_10pct"], 2)
        row["churns_flagged"] = f"{ew['either_caught']:.0%}"
        rows.append(row)
        print(row, flush=True)

    df = pd.DataFrame(rows)
    out = ROOT / "docs" / f"multi_seed_{args.label}.md"
    header = "| " + " | ".join(df.columns) + " |\n|" + "---|" * len(df.columns) + "\n"
    body = "".join("| " + " | ".join(str(v) for v in r) + " |\n" for r in df.itertuples(index=False))
    out.write_text(f"# Out-of-sample seeds ({args.label})\n\nDays = days from incident start to first matching alert.\n\n"
                   + header + body, encoding="utf-8", newline="\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
