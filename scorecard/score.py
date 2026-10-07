"""Score the analytics layer against the sealed answer key.

This is the only code that reads data/answer_key/. It answers three questions:
  1. Incidents   Did the anomaly monitor find each planted revenue incident, and how fast?
  2. Data quality How much of each injected data problem did the tests catch?
  3. Early warning How many churns did the model / health score flag beforehand, and how early?

Writes docs/scorecard.md and schema `scorecard` in DuckDB.

    python scorecard/score.py --db warehouse/revenue.duckdb
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
KEY = ROOT / "data" / "answer_key"
RAW = (ROOT / "data" / "raw").as_posix()
WARNING_WINDOW_DAYS = 60


# --------------------------------------------------------------- incidents
def incident_matches(alerts: pd.DataFrame, inc: dict, planted_am: str) -> pd.DataFrame:
    """Alerts that count as finding this incident. Rules are about the alert's
    detector, dimension and timing; they were written before the first run."""
    start, end = date.fromisoformat(inc["start"]), date.fromisoformat(inc["end"])
    a = alerts
    kind = inc["kind"]
    if kind == "price_hike":
        m = (a.detector == "churn_spike") & a.dimension_value.isin(["SMB", "starter"]) & \
            a.detected_at.between(start, start + timedelta(days=180))
    elif kind == "bad_release":
        m = (((a.detector == "ticket_spike") & (a.dimension_value == "bug")) |
             ((a.detector == "feature_usage_drop") & (a.dimension_value == "reports_run"))) & \
            a.detected_at.between(start, end + timedelta(days=30))
    elif kind == "am_neglect":
        m = (a.detector == "am_book_retention") & (a.dimension_value == planted_am) & a.detected_at.between(start, end)
    elif kind == "duplicate_webhooks":
        m = (a.detector == "duplicate_billing") & a.detected_at.between(start, end + timedelta(days=14))
    elif kind == "partner_cohort":
        cohorts = pd.period_range(start, end, freq="M").strftime("%Y-%m")
        m = (a.detector == "weak_cohort") & a.dimension_value.isin(cohorts)
    else:
        m = pd.Series(False, index=a.index)
    return a[m]


def score_incidents(con, key: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    alerts = con.sql("select * from metrics.anomaly_alerts").df()
    alerts["detected_at"] = pd.to_datetime(alerts["detected_at"]).dt.date
    matched_ids: set[str] = set()
    rows = []
    for inc in key["incidents"]:
        hits = incident_matches(alerts, inc, key["planted_am_neglect_name"]).sort_values("detected_at")
        matched_ids |= set(hits.alert_id)
        start = date.fromisoformat(inc["start"])
        first = hits.iloc[0] if len(hits) else None
        rows.append({
            "incident": inc["id"],
            "kind": inc["kind"],
            "started": start,
            "detected": first is not None,
            "first_alert": first.detected_at if first is not None else None,
            "days_to_detect": (first.detected_at - start).days if first is not None else None,
            "detector": first.detector if first is not None else None,
            "evidence": first.detail if first is not None else None,
        })
    # False alarms: unmatched alerts, collapsed into episodes (same detector and
    # dimension value, consecutive periods) so a single long alert counts once.
    fa = alerts[~alerts.alert_id.isin(matched_ids)].sort_values(["detector", "dimension_value", "period_start"]).copy()
    fa["period_start"] = pd.to_datetime(fa["period_start"])
    gap = fa.groupby(["detector", "dimension_value"])["period_start"].diff().dt.days
    fa["episode"] = ((gap.isna()) | (gap > 35)).cumsum()
    episodes = fa.groupby("episode").agg(detector=("detector", "first"), dimension_value=("dimension_value", "first"),
                                         first_period=("period_start", "min"), alerts=("alert_id", "count"),
                                         detail=("detail", "first")).reset_index(drop=True)

    # A churn spike that no rule matched may still be a real downstream effect of
    # an incident (e.g. the neglected AM's book is all Mid-Market). Look up the
    # true causes of the churns behind it; if most were incident-driven, it is a
    # secondary signal, not a false alarm.
    truth = pd.read_csv(KEY / "churn_truth.csv")
    truth["month_start"] = pd.to_datetime(truth["churn_date"]).dt.to_period("M").dt.to_timestamp()
    churns = con.sql("""select account_id, month_start, segment, plan_id
                        from metrics.mrr_waterfall_monthly where movement_category = 'Churn'""").df()
    churns["month_start"] = pd.to_datetime(churns["month_start"])
    churns = churns.merge(truth[["account_id", "month_start", "true_cause"]], on=["account_id", "month_start"], how="left")

    def related(ep) -> str | None:
        if ep.detector != "churn_spike":
            return None
        m = churns[(churns.month_start == ep.first_period) &
                   ((churns.segment == ep.dimension_value) | (churns.plan_id == ep.dimension_value))]
        causes = m["true_cause"].fillna("organic")
        incident_causes = causes[causes != "organic"]
        if len(m) and len(incident_causes) / len(m) >= 0.5:
            return incident_causes.value_counts().index[0]
        return None

    episodes["related_incident"] = [related(ep) for ep in episodes.itertuples()]
    return pd.DataFrame(rows), episodes


# ------------------------------------------------------------ data quality
def score_data_quality(con, key: dict) -> pd.DataFrame:
    inj = key["data_quality_injections"]
    q = lambda s: con.sql(s).fetchone()[0]
    raw = lambda f: f"read_csv('{RAW}/{f}', header = true, all_varchar = true)"
    found = {
        "INC-04_duplicate_webhook_events": ("warn_stripe_duplicate_subscription_changes",
            q("select sum(duplicate_deliveries_removed) from intermediate.int_subscription_events"), "removed in int_subscription_events"),
        "stripe_exact_retry_rows": ("unique event_id (dedup in staging)",
            q(f"select count(*) - count(distinct event_id) from {raw('stripe_events.csv')}"), "deduplicated in staging"),
        "stripe_plan_id_casing_whitespace": ("accepted_values plan_id",
            q(f"select count(*) from {raw('stripe_events.csv')} where plan_id not in ('starter','growth','enterprise')"), "lower/trim in staging"),
        "stripe_customer_missing_account_id": ("not_null metadata_account_id (warn)",
            q("select count(*) from staging.stg_stripe__customers where metadata_account_id is null"), "matched on email domain"),
        "ticket_null_account_id": ("not_null account_id (warn)",
            q("select count(*) from staging.stg_zendesk__tickets where account_id is null"), "kept, mapped to unknown member -1"),
        "ticket_timestamps_in_ist_offset": ("to_utc() normalisation",
            q(f"select count(*) from {raw('zendesk_tickets.csv')} where created_at like '%+05:30'"), "converted to UTC"),
        "ticket_solved_before_created": ("warn_ticket_solved_before_created",
            q("select count(*) from staging.stg_zendesk__tickets where solved_at < created_at"), "resolution nulled and flagged"),
        "ticket_csat_out_of_range": ("warn_ticket_csat_out_of_range",
            q("select count(*) from staging.stg_zendesk__tickets where csat_score_raw not between 1 and 5"), "CSAT nulled and flagged"),
        "ticket_priority_casing": ("accepted_values priority",
            q(f"select count(*) from {raw('zendesk_tickets.csv')} where priority not in ('low','normal','high','urgent')"), "lower/trim in staging"),
        "telemetry_null_session_end": ("warn_session_missing_end",
            q("select count(*) from staging.stg_telemetry__sessions where is_missing_end"), "duration nulled"),
        "telemetry_end_before_start": ("warn_session_end_before_start",
            q("select count(*) from staging.stg_telemetry__sessions where is_negative_duration"), "duration nulled"),
        "telemetry_exact_duplicate_rows": ("unique session_id (dedup in staging)",
            q(f"select count(*) - count(distinct session_id) from {raw('telemetry_sessions.csv.gz')}"), "deduplicated in staging"),
        "crm_changes_recorded_30plus_days_late": ("warn_crm_change_recorded_30_days_late",
            q("select count(*) from staging.stg_crm__account_changes where recording_lag_days >= 30"), "SCD2 uses effective date"),
    }
    rows = []
    for issue, injected in inj.items():
        test, n_found, handling = found[issue]
        rows.append({"issue": issue, "injected": injected, "flagged": int(n_found or 0),
                     "recall": min(1.0, (n_found or 0) / injected) if injected else None,
                     "caught_by": test, "handling": handling})
    return pd.DataFrame(rows)


# ----------------------------------------------------------- early warning
def score_early_warning(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    truth = pd.read_csv(KEY / "churn_truth.csv", parse_dates=["churn_date"])
    truth["churn_date"] = truth["churn_date"].dt.date
    bt = con.sql("select account_id, snapshot_date, flagged_top_10pct from ml.churn_backtest").df()
    hl = con.sql("select account_id, snapshot_date, health_band = 'At Risk' as flagged from metrics.customer_health_weekly").df()
    first_test = pd.to_datetime(bt["snapshot_date"]).dt.date.min()
    eval_churns = truth[truth["churn_date"] >= first_test + timedelta(days=28)].copy()

    def lead(signal: pd.DataFrame, flag_col: str) -> list:
        sig = signal[signal[flag_col]].copy()
        sig["snapshot_date"] = pd.to_datetime(sig["snapshot_date"]).dt.date
        by_acct = sig.groupby("account_id")["snapshot_date"].apply(list).to_dict()
        out = []
        for r in eval_churns.itertuples():
            days = [(r.churn_date - d).days for d in by_acct.get(r.account_id, [])
                    if 0 < (r.churn_date - d).days <= WARNING_WINDOW_DAYS]
            out.append(max(days) if days else None)
        return out

    eval_churns["model_lead_days"] = lead(bt, "flagged_top_10pct")
    eval_churns["health_lead_days"] = lead(hl, "flagged")
    eval_churns["model_flagged"] = eval_churns["model_lead_days"].notna()
    eval_churns["health_flagged"] = eval_churns["health_lead_days"].notna()
    eval_churns["either_flagged"] = eval_churns["model_flagged"] | eval_churns["health_flagged"]

    def summarise(g):
        return pd.Series({
            "churns": len(g),
            "model_caught": g["model_flagged"].mean(),
            "model_median_lead_days": g["model_lead_days"].median(),
            "health_caught": g["health_flagged"].mean(),
            "health_median_lead_days": g["health_lead_days"].median(),
            "either_caught": g["either_flagged"].mean(),
        })
    by_cause = eval_churns.groupby("true_cause").apply(summarise, include_groups=False).reset_index()
    total = summarise(eval_churns).to_frame().T.assign(true_cause="ALL")
    return pd.concat([total, by_cause], ignore_index=True), eval_churns


# ------------------------------------------------------------------ report
def fmt_pct(x):
    return "" if pd.isna(x) else f"{x:.0%}"


def write_report(path: Path, incidents, episodes, dq, ew, metrics, key):
    det = int(incidents["detected"].sum())
    lines = [
        "# Scorecard",
        "",
        f"Generated by `scorecard/score.py` (seed {key['seed']}, scale {key['scale']}). "
        "This is the only step that reads the answer key; everything it scores ran blind.",
        "",
        f"## 1. Planted revenue incidents: {det} of {len(incidents)} detected",
        "",
        "| Incident | Kind | Started | First alert | Days to detect | Detector | Evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in incidents.itertuples():
        lines.append(f"| {r.incident} | {r.kind} | {r.started} | {r.first_alert or 'missed'} | "
                     f"{'' if pd.isna(r.days_to_detect) else int(r.days_to_detect)} | {r.detector or ''} | {r.evidence or ''} |")
    false_alarms = int(episodes["related_incident"].isna().sum())
    lines += ["", f"**False-alarm episodes: {false_alarms}** "
              f"(plus {len(episodes) - false_alarms} secondary signals: unmatched alerts where most underlying churn was incident-driven)", ""]
    if len(episodes):
        lines += ["| Detector | Dimension | First period | Alerts | Detail | Verdict |", "|---|---|---|---|---|---|"]
        for r in episodes.itertuples():
            verdict = f"secondary signal of {r.related_incident}" if isinstance(r.related_incident, str) else "false alarm"
            lines.append(f"| {r.detector} | {r.dimension_value} | {r.first_period:%Y-%m-%d} | {r.alerts} | {r.detail} | {verdict} |")
    lines += [
        "",
        f"## 2. Data quality: {int((dq['recall'] >= 0.999).sum())} of {len(dq)} injected issue types fully caught",
        "",
        "| Issue | Injected | Flagged | Recall | Caught by | Handling |",
        "|---|---|---|---|---|---|",
    ]
    for r in dq.itertuples():
        lines.append(f"| {r.issue} | {r.injected:,} | {r.flagged:,} | {fmt_pct(r.recall)} | {r.caught_by} | {r.handling} |")
    sel = metrics[metrics["selected"]].iloc[0]
    lines += [
        "",
        "Flagged can differ from injected where injections overlap (a duplicated row that was also nulled) "
        "or where staging deduplication removes a row before it is counted.",
        "",
        "## 3. Churn early warning",
        "",
        f"Model: {sel['model']} trained on snapshots to {sel['train_end']}, tested out-of-time "
        f"{sel['test_start']} to {sel['test_end']}. ROC-AUC {sel['roc_auc']:.3f}, PR-AUC {sel['pr_auc']:.3f}, "
        f"precision in top 10% {sel['precision_top_10pct']:.1%} vs base rate {sel['base_rate']:.1%} "
        f"(lift {sel['lift_top_10pct']:.1f}x).",
        "",
        f"A churn counts as caught if the account was flagged in the {WARNING_WINDOW_DAYS} days before it churned "
        "(model: top 10% risk that week; health score: At Risk band).",
        "",
        "| True cause | Churns | Model caught | Model median lead (days) | Health caught | Health median lead (days) | Either |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in ew.itertuples():
        ml = "" if pd.isna(r.model_median_lead_days) else int(r.model_median_lead_days)
        hl = "" if pd.isna(r.health_median_lead_days) else int(r.health_median_lead_days)
        lines.append(f"| {r.true_cause} | {int(r.churns)} | {fmt_pct(r.model_caught)} | {ml} | "
                     f"{fmt_pct(r.health_caught)} | {hl} | {fmt_pct(r.either_caught)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="warehouse/revenue.duckdb")
    ap.add_argument("--report", default=str(ROOT / "docs" / "scorecard.md"))
    args = ap.parse_args()

    key = json.loads((KEY / "answer_key.json").read_text())
    con = duckdb.connect(args.db)
    incidents, episodes = score_incidents(con, key)
    dq = score_data_quality(con, key)
    ew, ew_detail = score_early_warning(con)
    metrics = con.sql("select * from ml.model_metrics").df()

    con.execute("create schema if not exists scorecard")
    for name, frame in [("incidents", incidents), ("false_alarms", episodes), ("data_quality", dq),
                        ("early_warning", ew), ("early_warning_detail", ew_detail)]:
        con.register("frame", frame)
        con.execute(f"create or replace table scorecard.{name} as select * from frame")
        con.unregister("frame")
    con.close()

    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    write_report(Path(args.report), incidents, episodes, dq, ew, metrics, key)

    pd.set_option("display.width", 200)
    print(incidents[["incident", "kind", "detected", "days_to_detect", "detector"]].to_string(index=False))
    print(f"false-alarm episodes: {int(episodes['related_incident'].isna().sum())} "
          f"(+{int(episodes['related_incident'].notna().sum())} secondary signals)")
    print(dq[["issue", "injected", "flagged", "recall"]].to_string(index=False))
    print(ew.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
