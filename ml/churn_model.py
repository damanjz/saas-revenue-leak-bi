"""60-day churn risk model.

1. Reads the dbt marts from DuckDB.
2. Builds one feature row per paying account per weekly snapshot, using only
   data available on the snapshot date (no look-ahead).
3. Trains logistic regression and XGBoost on an out-of-time split and keeps
   whichever has the better precision-recall AUC on the test period.
4. Writes back to DuckDB (schema `ml`):
     churn_risk_scores        current risk per active account with top 3 drivers
     churn_backtest           out-of-sample predictions for the test period
     model_metrics            test-period metrics for both models
     feature_importance       mean |SHAP| per feature

    python ml/churn_model.py --db warehouse/revenue.duckdb
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

HORIZON_DAYS = 60
ANALYSIS_END = date(2026, 9, 30)
TRAIN_END = date(2025, 12, 31)                      # last training snapshot; its label window closes 2026-03-01
TEST_START = TRAIN_END + timedelta(days=HORIZON_DAYS + 1)
LABELLED_END = ANALYSIS_END - timedelta(days=HORIZON_DAYS)   # last snapshot whose 60-day outcome is known
TOP_SHARE = 0.10                                    # CS team capacity: work the riskiest 10% of the book

CATEGORICALS = ["segment", "region", "acquisition_channel", "account_manager", "plan_id", "billing_interval"]
NUMERIC = [
    "mrr", "seats", "tenure_days", "days_to_renewal",
    "sessions_30d", "usage_velocity_delta", "adoption_rate_30d", "days_since_last_session",
    "reports_velocity_delta",
    "tickets_30d", "ticket_spike", "high_priority_tickets_30d", "bug_tickets_30d", "sla_breaches_30d",
    "csat_90d", "csat_trend", "csat_missing",
    "seat_change_90d", "price_increase_90d", "contractions_180d",
]

FEATURE_SQL = f"""
with snaps as (
    select account_id, date_day as snapshot_date, mrr, seats, plan_id, billing_interval
    from intermediate.int_account_mrr_daily
    where isodow(date_day) = 7 or date_day = date '{ANALYSIS_END}'
),

usage as (
    select
        s.account_id, s.snapshot_date,
        sum(case when u.usage_date > s.snapshot_date - 30 then u.sessions else 0 end)      as sessions_30d,
        sum(case when u.usage_date <= s.snapshot_date - 30 then u.sessions else 0 end)     as sessions_prev_30d,
        sum(case when u.usage_date > s.snapshot_date - 30 then u.active_users else 0 end) / 30.0 as avg_daily_active_users_30d,
        sum(case when u.usage_date > s.snapshot_date - 30 then u.reports_run else 0 end)   as reports_30d,
        sum(case when u.usage_date <= s.snapshot_date - 30 then u.reports_run else 0 end)  as reports_prev_30d,
        max(u.usage_date)                                                                  as last_usage_date
    from snaps as s
    left join core.fact_product_usage as u
        on u.account_id = s.account_id
       and u.usage_date > s.snapshot_date - 60
       and u.usage_date <= s.snapshot_date
    group by 1, 2
),

tickets as (
    select
        s.account_id, s.snapshot_date,
        count(*) filter (where t.created_at > s.snapshot_date - 30)                                        as tickets_30d,
        count(*) filter (where t.created_at <= s.snapshot_date - 30)                                       as tickets_prev_90d,
        count(*) filter (where t.created_at > s.snapshot_date - 30 and t.priority in ('high', 'urgent'))   as high_priority_tickets_30d,
        count(*) filter (where t.created_at > s.snapshot_date - 30 and t.is_bug)                           as bug_tickets_30d,
        count(*) filter (where t.created_at > s.snapshot_date - 30 and t.resolution_sla_breached)          as sla_breaches_30d,
        avg(t.csat_score) filter (where t.created_at > s.snapshot_date - 90)                               as csat_90d,
        avg(t.csat_score) filter (where t.created_at > s.snapshot_date - 45)                               as csat_recent,
        avg(t.csat_score) filter (where t.created_at <= s.snapshot_date - 45)                              as csat_prior
    from snaps as s
    left join core.fact_support_tickets as t
        on t.account_id = s.account_id
       and t.created_at > s.snapshot_date - 120
       and t.created_at <= s.snapshot_date + interval 1 day
    group by 1, 2
),

billing as (
    select
        s.account_id, s.snapshot_date,
        max(m.movement_date) filter (where m.movement_type in ('New', 'Reactivation'))                    as subscription_start,
        count(*) filter (where m.change_reason = 'price_change' and m.movement_date > s.snapshot_date - 90) > 0 as price_increase_90d,
        count(*) filter (where m.movement_type = 'Contraction' and m.movement_date > s.snapshot_date - 180) as contractions_180d
    from snaps as s
    join core.fact_mrr_movements as m
        on m.account_id = s.account_id
       and m.movement_date <= s.snapshot_date
    group by 1, 2
),

labels as (
    select s.account_id, s.snapshot_date, count(m.account_id) > 0 as churned_60d
    from snaps as s
    left join core.fact_mrr_movements as m
        on m.account_id = s.account_id
       and m.movement_type = 'Churn'
       and m.movement_date > s.snapshot_date
       and m.movement_date <= s.snapshot_date + {HORIZON_DAYS}
    group by 1, 2
)

select
    s.*,
    d.customer_sk, d.segment, d.region, d.acquisition_channel, d.account_manager,
    u.sessions_30d, u.sessions_prev_30d, u.avg_daily_active_users_30d, u.reports_30d, u.reports_prev_30d,
    date_diff('day', u.last_usage_date, s.snapshot_date)                as days_since_last_session,
    t.tickets_30d, t.tickets_prev_90d, t.high_priority_tickets_30d, t.bug_tickets_30d, t.sla_breaches_30d,
    t.csat_90d, t.csat_recent, t.csat_prior,
    b.subscription_start, b.price_increase_90d, b.contractions_180d,
    p.seats                                                             as seats_90d_ago,
    l.churned_60d
from snaps as s
left join usage as u using (account_id, snapshot_date)
left join tickets as t using (account_id, snapshot_date)
left join billing as b using (account_id, snapshot_date)
left join labels as l using (account_id, snapshot_date)
left join intermediate.int_account_mrr_daily as p
    on p.account_id = s.account_id and p.date_day = s.snapshot_date - 90
left join core.dim_customers as d
    on d.account_id = s.account_id and s.snapshot_date between d.valid_from and d.valid_to
"""


def engineer(raw: pd.DataFrame) -> pd.DataFrame:
    """Turn raw window aggregates into model features."""
    df = raw.copy()
    df["snapshot_date"] = pd.to_datetime(df["snapshot_date"]).dt.date
    df["tenure_days"] = (pd.to_datetime(df["snapshot_date"]) - pd.to_datetime(df["subscription_start"])).dt.days
    cycle = np.where(df["billing_interval"] == "year", 365.0, 30.4)
    df["days_to_renewal"] = (cycle - np.mod(df["tenure_days"], cycle)).round()

    # 30-day usage velocity: change in sessions vs the prior 30 days (-1 = usage stopped)
    df["usage_velocity_delta"] = (df["sessions_30d"] - df["sessions_prev_30d"]) / df["sessions_prev_30d"].clip(lower=1)
    df["reports_velocity_delta"] = (df["reports_30d"] - df["reports_prev_30d"]) / df["reports_prev_30d"].clip(lower=1)
    df["adoption_rate_30d"] = df["avg_daily_active_users_30d"] / df["seats"].clip(lower=1)
    df["days_since_last_session"] = df["days_since_last_session"].fillna(60)

    # ticket frequency spike: tickets this month minus the account's own monthly baseline
    df["ticket_spike"] = df["tickets_30d"] - df["tickets_prev_90d"] / 3.0

    # CSAT trend: last 45 days vs the 75 days before; missing responses are neutral and flagged
    df["csat_missing"] = df["csat_90d"].isna().astype(int)
    df["csat_trend"] = (df["csat_recent"] - df["csat_prior"]).fillna(0)
    df["csat_90d"] = df["csat_90d"].fillna(4.0)

    df["seat_change_90d"] = (df["seats"] - df["seats_90d_ago"]).fillna(0)
    df["price_increase_90d"] = df["price_increase_90d"].fillna(False).astype(int)
    df["contractions_180d"] = df["contractions_180d"].fillna(0)
    for c in ["sessions_30d", "tickets_30d", "high_priority_tickets_30d", "bug_tickets_30d", "sla_breaches_30d"]:
        df[c] = df[c].fillna(0)
    return df


def design_matrix(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    X = pd.concat([df[NUMERIC].astype(float), pd.get_dummies(df[CATEGORICALS].fillna("Unknown"), dtype=float)], axis=1)
    return X.reindex(columns=columns, fill_value=0.0) if columns is not None else X


def evaluate(name: str, y: np.ndarray, p: np.ndarray) -> dict:
    k = max(1, int(len(p) * TOP_SHARE))
    top = np.argsort(-p)[:k]
    return {
        "model": name,
        "roc_auc": roc_auc_score(y, p),
        "pr_auc": average_precision_score(y, p),
        "brier": brier_score_loss(y, p),
        "base_rate": y.mean(),
        "precision_top_10pct": y[top].mean(),
        "recall_top_10pct": y[top].sum() / max(y.sum(), 1),
        "lift_top_10pct": y[top].mean() / max(y.mean(), 1e-9),
        "test_rows": len(y),
        "test_churners": int(y.sum()),
    }


def fit_xgb(X: pd.DataFrame, y: np.ndarray) -> xgb.XGBClassifier:
    model = xgb.XGBClassifier(
        n_estimators=400, learning_rate=0.03, max_depth=4, min_child_weight=5,
        subsample=0.8, colsample_bytree=0.8, reg_lambda=2.0,
        scale_pos_weight=(len(y) - y.sum()) / max(y.sum(), 1),
        eval_metric="aucpr", random_state=42, n_jobs=4,
    )
    return model.fit(X, y)


def fit_logit(X: pd.DataFrame, y: np.ndarray):
    scaler = StandardScaler().fit(X)
    model = LogisticRegression(C=0.3, class_weight="balanced", max_iter=2000).fit(scaler.transform(X), y)
    return scaler, model


def explain(df: pd.DataFrame, row: pd.Series, feature: str) -> str:
    """Plain-English text for a driver, for the CS action list in Power BI."""
    v = row.get(feature)
    templates = {
        "usage_velocity_delta": lambda: f"Sessions {row['usage_velocity_delta']:+.0%} vs prior 30 days",
        "reports_velocity_delta": lambda: f"Reports usage {row['reports_velocity_delta']:+.0%} vs prior 30 days",
        "adoption_rate_30d": lambda: f"Only {row['adoption_rate_30d']:.0%} of seats active daily",
        "days_since_last_session": lambda: f"{int(row['days_since_last_session'])} days since last session",
        "sessions_30d": lambda: f"{int(row['sessions_30d'])} sessions in 30 days",
        "tickets_30d": lambda: f"{int(row['tickets_30d'])} tickets in 30 days",
        "ticket_spike": lambda: f"Tickets {row['ticket_spike']:+.0f} vs normal month",
        "high_priority_tickets_30d": lambda: f"{int(row['high_priority_tickets_30d'])} high/urgent tickets in 30 days",
        "bug_tickets_30d": lambda: f"{int(row['bug_tickets_30d'])} bug tickets in 30 days",
        "sla_breaches_30d": lambda: f"{int(row['sla_breaches_30d'])} SLA breaches in 30 days",
        "csat_90d": lambda: f"CSAT {row['csat_90d']:.1f}/5 over 90 days",
        "csat_trend": lambda: f"CSAT trend {row['csat_trend']:+.1f}",
        "csat_missing": lambda: "No CSAT responses in 90 days",
        "days_to_renewal": lambda: f"Renewal in {int(row['days_to_renewal'])} days",
        "price_increase_90d": lambda: "Price increase in last 90 days",
        "seat_change_90d": lambda: f"Seats {int(row['seat_change_90d']):+d} in 90 days",
        "contractions_180d": lambda: f"{int(row['contractions_180d'])} downgrades in 180 days",
        "tenure_days": lambda: f"Customer for {int(row['tenure_days'])} days",
        "mrr": lambda: f"MRR ${row['mrr']:,.0f}",
        "seats": lambda: f"{int(row['seats'])} seats",
    }
    if feature in templates:
        return templates[feature]()
    labels = {"segment": "Segment", "region": "Region", "acquisition_channel": "Channel",
              "account_manager": "Account manager", "plan_id": "Plan", "billing_interval": "Billed"}
    for cat in CATEGORICALS:
        if feature.startswith(cat + "_"):
            value = feature[len(cat) + 1:]
            if cat == "billing_interval":
                return "Billed annually" if value == "year" else "Billed monthly"
            if cat == "plan_id":
                return f"{value.capitalize()} plan"
            return f"{labels[cat]}: {value}"
    return f"{feature} = {v}"


def top_drivers(df: pd.DataFrame, contribs: np.ndarray, columns: list[str], n: int = 3) -> pd.DataFrame:
    rows = []
    for i in range(len(df)):
        c = contribs[i]
        order = [j for j in np.argsort(-c) if c[j] > 0][:n]
        rec = {}
        for rank, j in enumerate(order, start=1):
            rec[f"driver_{rank}"] = explain(df, df.iloc[i], columns[j])
            rec[f"driver_{rank}_impact"] = round(float(c[j]), 4)
        rows.append(rec)
    return pd.DataFrame(rows, index=df.index)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="warehouse/revenue.duckdb")
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    con.execute("set threads = 1")   # deterministic float sums, so reruns are byte-identical
    df = engineer(con.sql(FEATURE_SQL).df())
    df = df[df["tenure_days"].notna()].reset_index(drop=True)
    df["churned_60d"] = df["churned_60d"].astype(int)

    train = df[df["snapshot_date"] <= TRAIN_END]
    test = df[(df["snapshot_date"] >= TEST_START) & (df["snapshot_date"] <= LABELLED_END)]
    print(f"snapshots: train {len(train):,} (to {TRAIN_END}), test {len(test):,} ({TEST_START} to {LABELLED_END})")
    print(f"60-day churn base rate: train {train['churned_60d'].mean():.2%}, test {test['churned_60d'].mean():.2%}")

    X_train = design_matrix(train)
    cols = list(X_train.columns)
    X_test = design_matrix(test, cols)
    y_train, y_test = train["churned_60d"].to_numpy(), test["churned_60d"].to_numpy()

    xgb_model = fit_xgb(X_train, y_train)
    p_xgb = xgb_model.predict_proba(X_test)[:, 1]
    scaler, logit = fit_logit(X_train, y_train)
    p_log = logit.predict_proba(scaler.transform(X_test))[:, 1]

    metrics = pd.DataFrame([evaluate("xgboost", y_test, p_xgb), evaluate("logistic_regression", y_test, p_log)])
    best = metrics.sort_values("pr_auc", ascending=False).iloc[0]["model"]
    metrics["selected"] = metrics["model"] == best
    metrics["train_end"], metrics["test_start"], metrics["test_end"] = TRAIN_END, TEST_START, LABELLED_END
    print(metrics[["model", "roc_auc", "pr_auc", "precision_top_10pct", "recall_top_10pct", "lift_top_10pct"]]
          .round(3).to_string(index=False))
    print(f"selected: {best}")

    # Out-of-sample scores for every snapshot after training (labels unknown for the
    # last 60 days), used by the scorecard to measure early-warning lead time.
    after = df[df["snapshot_date"] >= TEST_START]
    X_after = design_matrix(after, cols)
    backtest = after[["account_id", "customer_sk", "snapshot_date", "mrr", "churned_60d"]].copy()
    backtest["label_known"] = backtest["snapshot_date"] <= LABELLED_END
    backtest["churn_probability"] = (xgb_model.predict_proba(X_after)[:, 1] if best == "xgboost"
                                     else logit.predict_proba(scaler.transform(X_after))[:, 1])
    backtest["risk_rank_in_snapshot"] = backtest.groupby("snapshot_date")["churn_probability"].rank(ascending=False, method="first")
    backtest["snapshot_accounts"] = backtest.groupby("snapshot_date")["account_id"].transform("count")
    backtest["flagged_top_10pct"] = backtest["risk_rank_in_snapshot"] <= np.ceil(backtest["snapshot_accounts"] * TOP_SHARE)
    backtest["model"] = best

    # Refit the chosen model on every labelled snapshot, then score today's book.
    labelled = df[df["snapshot_date"] <= LABELLED_END]
    X_all = design_matrix(labelled, cols)
    current = df[df["snapshot_date"] == ANALYSIS_END].reset_index(drop=True)
    X_cur = design_matrix(current, cols)
    if best == "xgboost":
        final = fit_xgb(X_all, labelled["churned_60d"].to_numpy())
        prob = final.predict_proba(X_cur)[:, 1]
        contribs = final.get_booster().predict(xgb.DMatrix(X_cur), pred_contribs=True)[:, :-1]
    else:
        scaler, final = fit_logit(X_all, labelled["churned_60d"].to_numpy())
        prob = final.predict_proba(scaler.transform(X_cur))[:, 1]
        contribs = scaler.transform(X_cur) * final.coef_[0]

    scores = current[["account_id", "customer_sk", "snapshot_date", "segment", "account_manager", "plan_id", "mrr", "seats"]].copy()
    scores["churn_probability"] = prob.round(4)
    scores["risk_rank"] = scores["churn_probability"].rank(ascending=False, method="first").astype(int)
    n = len(scores)
    scores["risk_tier"] = np.select(
        [scores["risk_rank"] <= np.ceil(n * TOP_SHARE), scores["risk_rank"] <= np.ceil(n * 0.25)],
        ["High", "Medium"], "Low")
    scores["mrr_at_risk"] = (scores["mrr"] * scores["churn_probability"]).round(2)
    scores = pd.concat([scores, top_drivers(current, contribs, cols)], axis=1)
    scores["model"] = best

    importance = pd.DataFrame({"feature": cols, "mean_abs_contribution": np.abs(contribs).mean(axis=0)})
    importance = importance.sort_values("mean_abs_contribution", ascending=False).reset_index(drop=True)

    con.execute("create schema if not exists ml")
    for name, frame in [("churn_risk_scores", scores), ("churn_backtest", backtest),
                        ("model_metrics", metrics), ("feature_importance", importance)]:
        con.register("frame", frame)
        con.execute(f"create or replace table ml.{name} as select * from frame")
        con.unregister("frame")
    con.close()

    print(f"scored {n} active accounts: {int((scores['risk_tier'] == 'High').sum())} high risk, "
          f"${scores.loc[scores['risk_tier'] == 'High', 'mrr_at_risk'].sum():,.0f} expected MRR at risk in that tier")
    print("top features:", ", ".join(importance["feature"].head(6)))


if __name__ == "__main__":
    main()
