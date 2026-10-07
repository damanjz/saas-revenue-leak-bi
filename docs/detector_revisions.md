# Detector revisions

The anomaly monitor (`dbt/models/marts/metrics/anomaly_alerts.sql`) was revised once, after the first
scored run. This note records what changed, why, and how the change was checked, because revising a
detector after seeing the answer key is exactly how a monitor ends up overfitted.

## v1: written blind

Six detectors with thresholds set before anything was scored:

| Detector | v1 rule |
|---|---|
| churn_spike | segment or plan monthly logo churn at least 2x its trailing 6-month average, at least 5 churns |
| ticket_spike | weekly tickets in a category above mean + 3 sd of the trailing 8 weeks, and at least 2x the mean |
| feature_usage_drop | weekly use of a feature per active account below 60% of its trailing 8-week mean |
| am_book_retention | an account manager's trailing 3-month GRR at least 5 points below segment peers, two months running |
| weak_cohort | cohort logo retention at month 3 or 6 below mean - 1.5 sd of earlier cohorts |
| duplicate_billing | 3 or more duplicate subscription events removed in a week |

The first scored run (seed 42) found 4 of the 5 planted incidents but raised 10 false-alarm episodes.
Reading those false alarms showed two design faults, not bad luck:

1. **Ad-hoc thresholds on small counts.** "2x the average" and "mean + 3 sd of 8 weeks" fire on noise
   when the counts are small: an account manager with a dozen customers in an early month, a ticket
   category that averages 4 a week.
2. **Too few cohort checkpoints.** The planted weak cohort only separates from its neighbours after
   month 6; checking months 3 and 6 alone could not see it.

## v2: statistical tests

| Detector | v2 rule | Why |
|---|---|---|
| churn_spike | Poisson z of at least 3 against the trailing 6-month rate, at least 2x expected, at least 5 churns | Scales the bar with the size of the group |
| ticket_spike | Poisson z of at least 4 against a 12-week mean (8-week warm-up), at least 2x the mean, at least 10 tickets | Longer baseline, count-based test |
| feature_usage_drop | unchanged | No false alarms in v1 |
| am_book_retention | two-proportion z of at least 3: share of customer-months lost to churn or downgrade over 6 months, against pooled segment peers, at least 60 customer-months | A proper test instead of a fixed 5-point gap |
| weak_cohort | binomial z of -2.5 or lower at months 3, 6, 9 and 12, against pooled earlier cohorts | Standard checkpoints; test scales with cohort size |
| duplicate_billing | unchanged | No false alarms in v1 |

## How the revision was checked

The thresholds above are no longer blind for seed 42, so seed 42 cannot judge them. Both versions were
run on three more simulated worlds (seeds 7, 2024 and 31337) that were never inspected before scoring.
The results are in [`multi_seed_v1.md`](multi_seed_v1.md) and [`multi_seed_v2.md`](multi_seed_v2.md),
and the comparison is in the case study. The scorecard rules that decide whether an alert "found" an
incident were written before the first run and were not changed.
