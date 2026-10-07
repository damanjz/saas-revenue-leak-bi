"""Run the whole project end to end.

    python pipeline.py                 # full run
    python pipeline.py --scale 0.25    # small run (CI)
    python pipeline.py --skip-generate # reuse existing raw files

Steps: simulate -> dbt build (models + tests) -> churn model -> Power BI
exports -> scorecard against the sealed answer key.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = Path(sys.executable).parent


def step(name: str, cmd: list[str], cwd: Path = ROOT, env: dict | None = None):
    print(f"\n=== {name} ===", flush=True)
    t0 = time.time()
    result = subprocess.run(cmd, cwd=cwd, env={**os.environ, **(env or {})})
    if result.returncode != 0:
        sys.exit(f"{name} failed (exit {result.returncode})")
    print(f"--- {name}: {time.time() - t0:.1f}s", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-generate", action="store_true")
    args = ap.parse_args()

    (ROOT / "warehouse").mkdir(exist_ok=True)
    db = ROOT / "warehouse" / "revenue.duckdb"
    dbt_env = {
        "DUCKDB_PATH": str(db),
        "RAW_DIR": (ROOT / "data" / "raw").as_posix(),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }

    if not args.skip_generate:
        step("1. simulate sources", [sys.executable, "-m", "generator",
                                      "--seed", str(args.seed), "--scale", str(args.scale), "--out", "data"])
        db.unlink(missing_ok=True)
    dbt = str(BIN / ("dbt.exe" if os.name == "nt" else "dbt"))
    step("2. dbt build (models + tests)", [dbt, "build", "--profiles-dir", "."], cwd=ROOT / "dbt", env=dbt_env)
    step("3. churn model", [sys.executable, "ml/churn_model.py", "--db", str(db)])
    step("4. Power BI exports", [sys.executable, "scripts/export_marts.py", "--db", str(db)])
    step("5. scorecard", [sys.executable, "scorecard/score.py", "--db", str(db)])


if __name__ == "__main__":
    main()
