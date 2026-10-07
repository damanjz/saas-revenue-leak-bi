-- MRR waterfall: one row per account per month, comparing month-end MRR with
-- the previous month-end and classifying the change.
--   New          0 -> >0, never paid before
--   Reactivation 0 -> >0, paid before
--   Expansion    up
--   Contraction  down, still paying
--   Churn        >0 -> 0
--   Retained     unchanged
-- Attributes (segment, AM, plan) are taken as of the START of the month for
-- churned accounts and as of the END of the month otherwise, so churn is
-- credited to the owner who lost it.
with months as (
    select distinct month_start, month_end
    from {{ ref('dim_date') }}
    where is_in_analysis_period
),

accounts as (
    select account_id, min(date_day) as first_day
    from {{ ref('int_account_mrr_daily') }}
    group by 1
),

grid as (
    select a.account_id, m.month_start, m.month_end
    from accounts as a
    join months as m on m.month_end >= a.first_day
),

month_end_state as (
    select
        g.account_id,
        g.month_start,
        g.month_end,
        coalesce(d.mrr_cents, 0) as mrr_cents,
        d.plan_id
    from grid as g
    left join {{ ref('int_account_mrr_daily') }} as d
        on d.account_id = g.account_id
       and d.date_day = g.month_end
),

sequenced as (
    select
        *,
        lag(mrr_cents, 1, 0) over w                              as prev_mrr_cents,
        lag(plan_id) over w                                      as prev_plan_id,
        lag(month_end) over w                                    as prev_month_end,
        coalesce(max(mrr_cents) over (
            partition by account_id order by month_start
            rows between unbounded preceding and 2 preceding
        ), 0) > 0                                                as paid_before_last_month
    from month_end_state
    window w as (partition by account_id order by month_start)
),

classified as (
    select
        *,
        case
            when prev_mrr_cents = 0 and mrr_cents > 0 and not paid_before_last_month then 'New'
            when prev_mrr_cents = 0 and mrr_cents > 0                                then 'Reactivation'
            when prev_mrr_cents > 0 and mrr_cents = 0                                then 'Churn'
            when mrr_cents > prev_mrr_cents                                          then 'Expansion'
            when mrr_cents < prev_mrr_cents                                          then 'Contraction'
            else 'Retained'
        end as movement_category
    from sequenced
    where mrr_cents > 0 or prev_mrr_cents > 0
)

select
    c.account_id || '-' || strftime(c.month_start, '%Y%m')                 as waterfall_id,
    c.account_id,
    c.month_start,
    {{ date_key('c.month_start') }}                                        as month_date_key,
    coalesce(dc.customer_sk, -1)                                           as customer_sk,
    dc.segment,
    dc.account_manager,
    dc.region,
    dc.cohort_month,
    coalesce(case when c.movement_category = 'Churn' then c.prev_plan_id else c.plan_id end, 'none') as plan_id,
    c.movement_category,
    c.prev_mrr_cents / 100.0                                               as starting_mrr,
    c.mrr_cents / 100.0                                                    as ending_mrr,
    case when c.movement_category = 'New'          then c.mrr_cents / 100.0 else 0 end              as new_mrr,
    case when c.movement_category = 'Reactivation' then c.mrr_cents / 100.0 else 0 end              as reactivation_mrr,
    case when c.movement_category = 'Expansion'    then (c.mrr_cents - c.prev_mrr_cents) / 100.0 else 0 end as expansion_mrr,
    case when c.movement_category = 'Contraction'  then (c.prev_mrr_cents - c.mrr_cents) / 100.0 else 0 end as contraction_mrr,
    case when c.movement_category = 'Churn'        then c.prev_mrr_cents / 100.0 else 0 end         as churned_mrr
from classified as c
left join {{ ref('dim_customers') }} as dc
    on dc.account_id = c.account_id
   and (case when c.movement_category = 'Churn' then c.prev_month_end else c.month_end end)
       between dc.valid_from and dc.valid_to
