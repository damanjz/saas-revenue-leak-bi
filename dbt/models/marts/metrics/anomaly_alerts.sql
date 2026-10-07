-- Revenue leak monitor. Six generic detectors, each comparing a metric with
-- its own trailing baseline using only data available at the time (no
-- look-ahead). Thresholds were set once, before scoring against the answer key.
--
--   churn_spike           segment/plan monthly logo churn >= 2x trailing 6-month rate (min 5 churns)
--   ticket_spike          weekly tickets in a category > mean + 3 sd of trailing 8 weeks and >= 2x mean
--   feature_usage_drop    weekly feature use per active account < 60% of trailing 8-week mean
--   am_book_retention     an AM's trailing-3-month GRR >= 5 pts below segment peers, 2 months running
--   weak_cohort           cohort logo retention at month 3 or 6 < mean - 1.5 sd of earlier cohorts
--   duplicate_billing     >= 3 duplicate subscription events removed in a week
{{ config(materialized='table') }}

with waterfall as (
    select * from {{ ref('mrr_waterfall_monthly') }}
),

-- 1. churn spike ------------------------------------------------------------
churn_by_dim as (
    select month_start, 'segment' as dimension, segment as dimension_value,
           count(*) filter (where starting_mrr > 0) as starting_customers,
           count(*) filter (where movement_category = 'Churn') as churned
    from waterfall group by 1, 2, 3
    union all
    select month_start, 'plan', plan_id,
           count(*) filter (where starting_mrr > 0),
           count(*) filter (where movement_category = 'Churn')
    from waterfall group by 1, 2, 3
),

churn_rates as (
    select
        *,
        churned * 1.0 / nullif(starting_customers, 0) as churn_rate,
        avg(churned * 1.0 / nullif(starting_customers, 0)) over w as baseline_rate,
        count(*) over w as baseline_months
    from churn_by_dim
    window w as (partition by dimension, dimension_value order by month_start
                 rows between 6 preceding and 1 preceding)
),

churn_spike as (
    select
        'churn_spike' as detector, month_start as period_start, dimension, dimension_value,
        churn_rate as observed_value, baseline_rate as baseline_value,
        churned || ' of ' || starting_customers || ' ' || dimension_value || ' customers churned' as detail
    from churn_rates
    where churned >= 5 and baseline_months >= 3 and churn_rate >= 2 * baseline_rate
),

-- 2. ticket spike -----------------------------------------------------------
weeks as (
    select distinct week_start from {{ ref('dim_date') }}
    where is_in_analysis_period
      and week_start + interval 6 day <= date '{{ var("analysis_end_date") }}'
      and week_start >= date '{{ var("analysis_start_date") }}'
),

ticket_weeks as (
    select w.week_start, c.category, count(t.ticket_id) as tickets
    from weeks as w
    cross join (select distinct category from {{ ref('fact_support_tickets') }}) as c
    left join {{ ref('fact_support_tickets') }} as t
        on t.category = c.category
       and cast(date_trunc('week', t.created_at) as date) = w.week_start
    group by 1, 2
),

ticket_stats as (
    select
        *,
        avg(tickets) over w as baseline_mean,
        stddev_samp(tickets) over w as baseline_sd,
        count(*) over w as baseline_weeks
    from ticket_weeks
    window w as (partition by category order by week_start rows between 8 preceding and 1 preceding)
),

ticket_spike as (
    select
        'ticket_spike', week_start, 'ticket_category', category,
        tickets, baseline_mean,
        tickets || ' ' || category || ' tickets vs ' || round(baseline_mean, 1) || ' weekly average'
    from ticket_stats
    where baseline_weeks >= 4 and tickets >= 10
      and tickets > baseline_mean + 3 * baseline_sd and tickets >= 2 * baseline_mean
),

-- 3. feature usage drop -----------------------------------------------------
usage_weeks as (
    select
        u.week_start,
        count(distinct u.account_id) as active_accounts,
        sum(u.dashboards_viewed) as dashboards_viewed,
        sum(u.reports_run) as reports_run,
        sum(u.integrations_synced) as integrations_synced,
        sum(u.exports) as exports,
        sum(u.api_calls) as api_calls
    from {{ ref('fact_product_usage_weekly') }} as u
    join weeks as w using (week_start)
    group by 1
),

feature_long as (
    unpivot usage_weeks
    on dashboards_viewed, reports_run, integrations_synced, exports, api_calls
    into name feature value total
),

feature_stats as (
    select
        week_start, feature,
        total * 1.0 / active_accounts as per_account,
        avg(total * 1.0 / active_accounts) over w as baseline,
        count(*) over w as baseline_weeks
    from feature_long
    window w as (partition by feature order by week_start rows between 8 preceding and 1 preceding)
),

feature_drop as (
    select
        'feature_usage_drop', week_start, 'feature', feature,
        per_account, baseline,
        feature || ' per active account fell to ' || round(100 * per_account / baseline) || '% of baseline'
    from feature_stats
    where baseline_weeks >= 4 and per_account < 0.6 * baseline
),

-- 4. account manager book retention -----------------------------------------
am_month as (
    select
        month_start, segment, account_manager,
        sum(starting_mrr) as starting_mrr,
        sum(churned_mrr + contraction_mrr) as lost_mrr,
        count(*) filter (where starting_mrr > 0) as starting_customers
    from waterfall
    where account_manager is not null and starting_mrr > 0
    group by 1, 2, 3
),

am_t3m as (
    select
        *,
        1 - sum(lost_mrr) over w / nullif(sum(starting_mrr) over w, 0) as grr_t3m,
        sum(starting_customers) over w as customers_t3m
    from am_month
    window w as (partition by segment, account_manager order by month_start
                 rows between 2 preceding and current row)
),

am_gap as (
    select
        a.*,
        (select avg(p.grr_t3m) from am_t3m as p
          where p.segment = a.segment and p.month_start = a.month_start
            and p.account_manager <> a.account_manager) as peer_grr_t3m
    from am_t3m as a
),

am_flagged as (
    select
        *,
        grr_t3m - peer_grr_t3m as gap,
        lag(grr_t3m - peer_grr_t3m) over (partition by segment, account_manager order by month_start) as prev_gap
    from am_gap
    where customers_t3m >= 20
),

am_alerts as (
    select
        'am_book_retention', month_start, 'account_manager', account_manager,
        grr_t3m, peer_grr_t3m,
        account_manager || ' (' || segment || ') 3-month GRR ' || round(100 * grr_t3m, 1)
            || '% vs peers ' || round(100 * peer_grr_t3m, 1) || '%'
    from am_flagged
    where gap <= -0.05 and prev_gap <= -0.05
),

-- 5. weak cohort ------------------------------------------------------------
cohort_points as (
    select
        cohort_month, months_since_signup, logo_retention,
        avg(logo_retention) over w as baseline,
        stddev_samp(logo_retention) over w as baseline_sd,
        count(*) over w as prior_cohorts
    from {{ ref('cohort_retention') }}
    where months_since_signup in (3, 6)
    window w as (partition by months_since_signup order by cohort_month
                 rows between unbounded preceding and 1 preceding)
),

weak_cohort as (
    select
        'weak_cohort',
        cast(cohort_month + to_months(months_since_signup + 1) as date),  -- first date the point is known
        'signup_cohort', strftime(cohort_month, '%Y-%m'),
        logo_retention, baseline,
        strftime(cohort_month, '%Y-%m') || ' cohort month-' || months_since_signup || ' retention '
            || round(100 * logo_retention) || '% vs ' || round(100 * baseline) || '% for earlier cohorts'
    from cohort_points
    where prior_cohorts >= 4 and logo_retention < baseline - 1.5 * baseline_sd
      and cohort_month + to_months(months_since_signup + 1) <= date '{{ var("analysis_end_date") }}'
),

-- 6. duplicate billing events -----------------------------------------------
dup_weeks as (
    select
        cast(date_trunc('week', event_date) as date) as week_start,
        sum(duplicate_deliveries_removed) as duplicates
    from {{ ref('int_subscription_events') }}
    group by 1
),

duplicate_billing as (
    select
        'duplicate_billing', week_start, 'source', 'stripe_webhooks',
        duplicates, 0.0,
        duplicates || ' duplicate subscription events removed'
    from dup_weeks
    where duplicates >= 3
),

all_alerts as (
    select * from churn_spike
    union all select * from ticket_spike
    union all select * from feature_drop
    union all select * from am_alerts
    union all select * from weak_cohort
    union all select * from duplicate_billing
)

select
    detector || '|' || dimension_value || '|' || strftime(period_start, '%Y-%m-%d') as alert_id,
    detector,
    cast(period_start as date)          as period_start,
    -- the first day the alert could actually have fired: the period has to be complete
    cast(case
        when detector in ('churn_spike', 'am_book_retention') then last_day(period_start)
        when detector in ('ticket_spike', 'feature_usage_drop', 'duplicate_billing') then period_start + interval 6 day
        else period_start
    end as date)                        as detected_at,
    {{ date_key('period_start') }}      as period_date_key,
    dimension,
    dimension_value,
    round(observed_value, 4)            as observed_value,
    round(baseline_value, 4)            as baseline_value,
    detail
from all_alerts
order by period_start, detector
