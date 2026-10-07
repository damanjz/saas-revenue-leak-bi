"""Simulation parameters.

Everything the generator does is driven from here, so the planted incidents
and the dirty-data rates are visible in one place. The analytics layer (dbt,
ML, Power BI) never reads this file or the answer key it produces.
"""
from dataclasses import dataclass, field
from datetime import date

START = date(2024, 10, 1)
END = date(2026, 9, 30)          # inclusive, 24 months

SEGMENTS = ["SMB", "Mid-Market", "Enterprise"]
SEGMENT_MIX = [0.60, 0.30, 0.10]
SEATS_RANGE = {"SMB": (2, 12), "Mid-Market": (12, 45), "Enterprise": (50, 200)}

PLANS = ["starter", "growth", "enterprise"]
LIST_PRICE = {"starter": 29, "growth": 59, "enterprise": 99}   # USD per seat per month
ANNUAL_DISCOUNT = 0.15
PLAN_MIX = {                                                    # by segment
    "SMB": [0.70, 0.30, 0.00],
    "Mid-Market": [0.20, 0.70, 0.10],
    "Enterprise": [0.00, 0.15, 0.85],
}
ANNUAL_SHARE = {"SMB": 0.10, "Mid-Market": 0.40, "Enterprise": 0.80}

# Account managers: pooled by segment. AM index 3 is the planted "neglect" book.
AM_POOLS = {"SMB": [0, 1, 2], "Mid-Market": [3, 4, 5], "Enterprise": [6, 7]}
N_AMS = 8

REGIONS = ["North America", "EMEA", "APAC", "LATAM"]
REGION_MIX = [0.48, 0.30, 0.16, 0.06]
INDUSTRIES = ["Software", "Financial Services", "Healthcare", "Retail", "Manufacturing",
              "Media", "Education", "Logistics", "Professional Services"]
CHANNELS = ["self-serve", "direct-sales", "partner"]

# New logos per month ramps linearly between these two values.
SIGNUPS_FIRST_MONTH = 30
SIGNUPS_LAST_MONTH = 60

# Monthly churn base rate at renewal, by segment (scaled by health).
CHURN_BASE_MONTHLY = {"SMB": 0.035, "Mid-Market": 0.020, "Enterprise": 0.012}
CHURN_BASE_ANNUAL = 0.14
REACTIVATION_MONTHLY_P = 0.025
REACTIVATION_WINDOW_MONTHS = 12

SLA_HOURS = {"urgent": 4, "high": 8, "normal": 24, "low": 72}


@dataclass(frozen=True)
class Incident:
    id: str
    kind: str
    start: date
    end: date
    scope: str
    expected_signal: str
    params: dict = field(default_factory=dict)


# The answer key. The pipeline has to find these blind.
INCIDENTS = [
    Incident(
        id="INC-01", kind="price_hike",
        start=date(2025, 11, 1), end=date(2026, 3, 31),
        scope="Starter plan list price 29 -> 39 USD/seat, applied at each account's next renewal",
        expected_signal="Churn spike in SMB / Starter accounts in the months after the change",
        params={"plan": "starter", "new_price": 39, "shock_churn_p": [0.08, 0.04, 0.04]},
    ),
    Incident(
        id="INC-02", kind="bad_release",
        start=date(2026, 3, 10), end=date(2026, 3, 31),
        scope="Release 5.2 breaks the Reports feature for reports-heavy accounts for 21 days",
        expected_signal="Bug ticket spike, Reports usage collapse, health drop, churn 1-3 months later",
        params={"extra_ticket_rate": 0.08, "usage_multiplier": 0.3, "health_hit": 0.30},
    ),
    Incident(
        id="INC-03", kind="am_neglect",
        start=date(2025, 6, 1), end=END,
        scope="One Mid-Market account manager stops working their book",
        expected_signal="That AM's book shows outlier NRR / contraction / churn vs peers",
        params={"am_index": 3, "max_health_hit": 0.35, "ramp_per_day": 0.004},
    ),
    Incident(
        id="INC-04", kind="duplicate_webhooks",
        start=date(2026, 1, 15), end=date(2026, 1, 31),
        scope="Stripe webhook retry bug re-delivers subscription.updated events with new event IDs",
        expected_signal="Duplicate subscription changes; naive MRR overstates expansion in Jan 2026",
        params={"dup_probability": 0.9},
    ),
    Incident(
        id="INC-05", kind="partner_cohort",
        start=date(2025, 4, 1), end=date(2025, 6, 30),
        scope="Partner launch brings in poorly qualified accounts whose usage decays after onboarding",
        expected_signal="2025-04..06 signup cohorts retain far worse than neighbouring cohorts",
        params={"partner_share": 0.45, "decay_start_days": 45, "decay_per_day": 0.003, "floor": 0.20},
    ),
]

# Dirty-data rates (data quality issues, not business incidents).
DIRTY = {
    "telemetry_exact_duplicate_rate": 0.008,
    "telemetry_null_duration_rate": 0.010,
    "telemetry_negative_duration_rate": 0.002,
    "ticket_null_account_rate": 0.005,
    "ticket_ist_offset_rate": 0.02,
    "ticket_solved_before_created_rate": 0.002,
    "ticket_csat_out_of_range_rate": 0.003,
    "ticket_priority_casing_rate": 0.05,
    "stripe_exact_retry_rate": 0.005,
    "stripe_plan_casing_rate": 0.03,
    "stripe_customer_missing_metadata_rate": 0.01,
    "crm_late_record_rate": 0.03,
}
