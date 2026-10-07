-- Monthly signup cohorts x months since first payment, with logo and revenue
-- retention. Feeds the cohort heatmap (cohort_month on rows, months_since on columns).
with cohorts as (
    select account_id, cohort_month
    from {{ ref('dim_customers') }}
    where is_current and cohort_month is not null
),

activity as (
    select
        c.cohort_month,
        w.account_id,
        w.month_start,
        cast(date_diff('month', c.cohort_month, w.month_start) as integer) as months_since_signup,
        w.ending_mrr
    from {{ ref('mrr_waterfall_monthly') }} as w
    join cohorts as c using (account_id)
),

cohort_base as (
    select
        cohort_month,
        count(distinct account_id)  as cohort_size,
        sum(ending_mrr)             as cohort_starting_mrr
    from activity
    where months_since_signup = 0
    group by 1
)

select
    a.cohort_month,
    strftime(a.cohort_month, '%Y-%m')                                       as cohort_label,
    a.months_since_signup,
    b.cohort_size,
    b.cohort_starting_mrr,
    count(distinct a.account_id) filter (where a.ending_mrr > 0)            as active_customers,
    sum(a.ending_mrr)                                                       as cohort_mrr,
    count(distinct a.account_id) filter (where a.ending_mrr > 0) * 1.0 / b.cohort_size as logo_retention,
    sum(a.ending_mrr) / nullif(b.cohort_starting_mrr, 0)                    as revenue_retention
from activity as a
join cohort_base as b using (cohort_month)
group by a.cohort_month, a.months_since_signup, b.cohort_size, b.cohort_starting_mrr
order by a.cohort_month, a.months_since_signup
