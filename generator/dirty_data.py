"""Inject realistic data quality problems into the clean simulated sources.

Every injection is counted and written to the answer key, so the dbt tests can
be scored on how much of the planted mess they catch.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from . import config as C


def _pick(rng, n: int, rate: float) -> np.ndarray:
    return np.where(rng.random(n) < rate)[0]


def inject(frames: dict[str, pd.DataFrame], seed: int) -> tuple[dict[str, pd.DataFrame], dict]:
    rng = np.random.default_rng(seed + 1)
    d = C.DIRTY
    log: dict[str, int] = {}

    # ---------------------------------------------------------------- Stripe
    ev = frames["stripe_events"].copy()
    inc = next(i for i in C.INCIDENTS if i.kind == "duplicate_webhooks")
    ts = pd.to_datetime(ev["occurred_at"], utc=True)
    in_window = (ev["event_type"] == "customer.subscription.updated") & \
                (ts.dt.date >= inc.start) & (ts.dt.date <= inc.end)
    cand = ev[in_window]
    dup = cand[rng.random(len(cand)) < inc.params["dup_probability"]].copy()
    dup["event_id"] = [f"evt_r{rng.integers(16**12, 16**13):013x}" for _ in range(len(dup))]
    log["INC-04_duplicate_webhook_events"] = len(dup)

    retry = ev.iloc[_pick(rng, len(ev), d["stripe_exact_retry_rate"])]
    log["stripe_exact_retry_rows"] = len(retry)
    ev = pd.concat([ev, dup, retry], ignore_index=True)

    idx = _pick(rng, len(ev), d["stripe_plan_casing_rate"])
    ev.loc[idx, "plan_id"] = [rng.choice([p.title(), f" {p}", p.upper()]) for p in ev.loc[idx, "plan_id"]]
    log["stripe_plan_id_casing_whitespace"] = len(idx)
    frames["stripe_events"] = ev.sort_values("occurred_at", kind="stable").reset_index(drop=True)

    cust = frames["stripe_customers"].copy()
    idx = _pick(rng, len(cust), d["stripe_customer_missing_metadata_rate"])
    cust["metadata_account_id"] = cust["metadata_account_id"].astype(object)
    cust.loc[idx, "metadata_account_id"] = None
    log["stripe_customer_missing_account_id"] = len(idx)
    frames["stripe_customers"] = cust

    # --------------------------------------------------------------- Zendesk
    tk = frames["zendesk_tickets"].copy()
    for col in ["organization_external_id", "created_at", "first_response_at", "solved_at", "priority"]:
        tk[col] = tk[col].astype(object)
    tk["csat_score"] = tk["csat_score"].astype("Int64").astype(object)

    idx = _pick(rng, len(tk), d["ticket_null_account_rate"])
    tk.loc[idx, "organization_external_id"] = None
    log["ticket_null_account_id"] = len(idx)

    idx = _pick(rng, len(tk), d["ticket_ist_offset_rate"])
    for col in ["created_at", "first_response_at"]:
        tk.loc[idx, col] = [
            (pd.Timestamp(v) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%dT%H:%M:%S+05:30")
            for v in tk.loc[idx, col]
        ]
    log["ticket_timestamps_in_ist_offset"] = len(idx)

    solved = tk.index[tk["solved_at"].notna()].to_numpy()
    idx = solved[_pick(rng, len(solved), d["ticket_solved_before_created_rate"])]
    tk.loc[idx, "solved_at"] = [
        (pd.Timestamp(c) - timedelta(hours=int(rng.integers(1, 48)))).strftime("%Y-%m-%dT%H:%M:%SZ")
        for c in pd.to_datetime(tk.loc[idx, "created_at"], utc=True, format="ISO8601")
    ]
    log["ticket_solved_before_created"] = len(idx)

    rated = tk.index[tk["csat_score"].notna()].to_numpy()
    idx = rated[_pick(rng, len(rated), d["ticket_csat_out_of_range_rate"])]
    tk.loc[idx, "csat_score"] = rng.choice([0, 6, 10], size=len(idx))
    log["ticket_csat_out_of_range"] = len(idx)

    idx = _pick(rng, len(tk), d["ticket_priority_casing_rate"])
    tk.loc[idx, "priority"] = [rng.choice([p.title(), p.upper(), f"{p} "]) for p in tk.loc[idx, "priority"]]
    log["ticket_priority_casing"] = len(idx)
    frames["zendesk_tickets"] = tk

    # ------------------------------------------------------------- Telemetry
    se = frames["telemetry_sessions"]
    idx = _pick(rng, len(se), d["telemetry_null_duration_rate"])
    se["session_end"] = se["session_end"].astype(object)
    se.loc[idx, "session_end"] = None
    log["telemetry_null_session_end"] = len(idx)

    idx = _pick(rng, len(se), d["telemetry_negative_duration_rate"])
    se.loc[idx, "session_end"] = se.loc[idx, "session_start"] - pd.to_timedelta(rng.integers(60, 3600, len(idx)), unit="s")
    log["telemetry_end_before_start"] = len(idx)

    dups = se.iloc[_pick(rng, len(se), d["telemetry_exact_duplicate_rate"])]
    log["telemetry_exact_duplicate_rows"] = len(dups)
    frames["telemetry_sessions"] = pd.concat([se, dups], ignore_index=True)

    # ------------------------------------------------------------------- CRM
    crm = frames["crm_account_changes"]
    lag = (pd.to_datetime(crm["recorded_at"]) - pd.to_datetime(crm["effective_at"])).dt.days
    log["crm_changes_recorded_30plus_days_late"] = int((lag >= 30).sum())

    return frames, log
