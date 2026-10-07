-- Revenue leak monitor (detector v2). Six generic detectors, each comparing a
-- metric with its own trailing baseline using only data available at the time
-- (no look-ahead).
--
--   churn_spike           segment/plan monthly churns vs trailing 6-month rate: Poisson z >= 3, >= 2x expected, >= 5 churns
--   ticket_spike          weekly tickets in a category vs trailing 12 weeks: Poisson z >= 4, >= 2x mean, >= 10 tickets
--   feature_usage_drop    weekly feature use per active account < 60% of trailing 8-week mean
--   am_book_retention     an AM's 6-month customer loss rate (churn + downgrade) vs segment peers: two-proportion z >= 3
--   weak_cohort           cohort logo retention at month 3/6/9/12 vs pooled earlier cohorts: binomial z <= -2.5
--   duplicate_billing     >= 3 duplicate subscription events removed in a week
--
-- v1 (blind) used ad-hoc thresholds and produced 10-14 false alarms per run;
-- see docs/detector_revisions.md for what changed and why.
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
        sum(churned) over w * 1.0 / nullif(sum(starting_customers) over w, 0) as baseline_rate,
        count(*) over w as baseline_months
    from churn_by_dim
    window w as (partition by dimension, dimension_value order by month_start
                 rows between 6 preceding and 1 preceding)
),

churn_spike as (
    select
        'churn_spike' as detector, month_start as period_start, dimension, dimension_value,
        churn_rate as observed_value, baseline_rate as baseline_value,
        churned || ' of ' || starting_customers || ' ' || dimension_value || ' customers churned vs '
            || round(expected, 1) || ' expected' as detail
    from (
        select *, greatest(baseline_rate * starting_customers, 0.5) as expected
        from churn_rates
    )
    where churned >= 5 and baseline_months >= 3
      and churned >= 2 * expected
      and (churned - expected) / sqrt(expected) >= 3
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
        count(*) over w as baseline_weeks
    from ticket_weeks
    window w as (partition by category order by week_start rows between 12 preceding and 1 preceding)
),

ticket_spike as (
    select
        'ticket_spike', week_start, 'ticket_category', category,
        tickets, baseline_mean,
        tickets || ' ' || category || ' tickets vs ' || round(baseline_mean, 1) || ' weekly average'
    from ticket_stats
    where baseline_weeks >= 8 and tickets >= 10 and tickets >= 2 * baseline_mean
      and (tickets - baseline_mean) / sqrt(greatest(baseline_mean, 1)) >= 4
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
        count(*) as starting_customers,
        count(*) filter (where movement_category in ('Churn', 'Contraction')) as lost_customers
    from waterfall
    where account_manager is not null and starting_mrr > 0
    group by 1, 2, 3
),

am_t6m as (
    select
        *,
        sum(starting_customers) over w as n_am,
        sum(lost_customers) over w as lost_am
    from am_month
    window w as (partition by segment, account_manager order by month_start
                 rows between 5 preceding and current row)
),

am_vs_peers as (
    select
        a.*,
        sum(p.n_am) as n_peer,
        sum(p.lost_am) as lost_peer
    from am_t6m as a
    join am_t6m as p
      on p.segment = a.segment and p.month_start = a.month_start and p.account_manager <> a.account_manager
    group by all
),

am_tested as (
    select
        *,
        lost_am * 1.0 / n_am as loss_rate,
        lost_peer * 1.0 / n_peer as peer_loss_rate,
        (lost_am + lost_peer) * 1.0 / (n_am + n_peer) as pooled
    from am_vs_peers
    where n_am >= 60
),

am_alerts as (
    select
        'am_book_retention', month_start, 'account_manager', account_manager,
        loss_rate, peer_loss_rate,
        account_manager || ' (' || segment || '): ' || round(100 * loss_rate, 1)
            || '% of book lost in 6 months vs ' || round(100 * peer_loss_rate, 1) || '% for peers'
    from am_tested
    where (loss_rate - peer_loss_rate)
          / sqrt(pooled * (1 - pooled) * (1.0 / n_am + 1.0 / n_peer)) >= 3
),

-- 5. weak cohort ------------------------------------------------------------
cohort_points as (
    select
        cohort_month, months_since_signup, cohort_size, active_customers, logo_retention,
        sum(active_customers) over w * 1.0 / nullif(sum(cohort_size) over w, 0) as baseline,
        count(*) over w as prior_cohorts
    from {{ ref('cohort_retention') }}
    where months_since_signup in (3, 6, 9, 12)
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
    where prior_cohorts >= 4
      and (logo_retention - baseline) / sqrt(baseline * (1 - baseline) / cohort_size) <= -2.5
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
