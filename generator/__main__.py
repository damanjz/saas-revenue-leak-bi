"""Generate the raw landing files and the sealed answer key.

    python -m generator --seed 42 --scale 1.0 --out data
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from . import config as C
from .dirty_data import inject
from .simulate import Simulation

RAW_FILES = {
    "stripe_events": "stripe_events.csv",
    "stripe_customers": "stripe_customers.csv",
    "zendesk_tickets": "zendesk_tickets.csv",
    "telemetry_sessions": "telemetry_sessions.csv.gz",
    "crm_account_changes": "crm_account_changes.csv",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scale", type=float, default=1.0, help="multiplier on new-logo volume (CI uses 0.25)")
    ap.add_argument("--out", type=Path, default=Path("data"))
    args = ap.parse_args()

    t0 = time.time()
    sim = Simulation(seed=args.seed, scale=args.scale).run()
    frames = sim.frames()
    frames, dq_log = inject(frames, args.seed)

    raw = args.out / "raw"
    key = args.out / "answer_key"
    raw.mkdir(parents=True, exist_ok=True)
    key.mkdir(parents=True, exist_ok=True)

    for name, fname in RAW_FILES.items():
        # mtime=0 keeps the gzip header free of timestamps, so reruns are byte-identical
        compression = {"method": "gzip", "mtime": 0} if fname.endswith(".gz") else None
        frames[name].to_csv(raw / fname, index=False, compression=compression)

    frames["churn_truth"].to_csv(key / "churn_truth.csv", index=False)
    frames["account_truth"].to_csv(key / "account_truth.csv", index=False)
    (key / "answer_key.json").write_text(json.dumps({
        "seed": args.seed,
        "scale": args.scale,
        "period": [C.START.isoformat(), C.END.isoformat()],
        "planted_am_neglect_name": sim.am_names[next(i for i in C.INCIDENTS if i.kind == "am_neglect").params["am_index"]],
        "incidents": [{**asdict(i), "start": i.start.isoformat(), "end": i.end.isoformat()} for i in C.INCIDENTS],
        "data_quality_injections": dq_log,
    }, indent=2), encoding="utf-8", newline="\n")

    print(f"accounts: {sim.n:,}   ({time.time() - t0:.1f}s)")
    for name, fname in RAW_FILES.items():
        print(f"  {fname:<28} {len(frames[name]):>10,} rows")
    print(f"  churn events (truth)         {len(frames['churn_truth']):>10,}")
    print(f"  data quality injections      {sum(dq_log.values()):>10,}")


if __name__ == "__main__":
    main()
