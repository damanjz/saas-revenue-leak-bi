-- Company-level MRR bridge with Net and Gross Revenue Retention.
--   NRR (MoM) = (start + expansion - contraction - churn) / start
--   GRR (MoM) = (start - contraction - churn) / start            (capped at 100%)
--   NRR (T12M) = MRR today from accounts that paid 12 months ago / their MRR then
with bridge as (
    select
        month_start,
        sum(starting_mrr)                                       as starting_mrr,
        sum(new_mrr)                                            as new_mrr,
        sum(reactivation_mrr)                                   as reactivation_mrr,
        sum(expansion_mrr)                                      as expansion_mrr,
        sum(contraction_mrr)                                    as contraction_mrr,
        sum(churned_mrr)                                        as churned_mrr,
        sum(ending_mrr)                                         as ending_mrr,
        count(*) filter (where starting_mrr > 0)                as starting_customers,
        count(*) filter (where movement_category = 'New')       as new_customers,
        count(*) filter (where movement_category = 'Reactivation') as reactivated_customers,
        count(*) filter (where movement_category = 'Churn')     as churned_customers,
        count(*) filter (where ending_mrr > 0)                  as ending_customers
    from {{ ref('mrr_waterfall_monthly') }}
    group by 1
),

trailing_12m as (
    -- left join: accounts that churned since have no row 12 months later and count as 0
    select
        cast(base.month_start + interval 12 month as date)                      as month_start,
        sum(coalesce(cur.ending_mrr, 0)) / nullif(sum(base.ending_mrr), 0)      as nrr_t12m,
        sum(least(coalesce(cur.ending_mrr, 0), base.ending_mrr)) / nullif(sum(base.ending_mrr), 0) as grr_t12m
    from {{ ref('mrr_waterfall_monthly') }} as base
    left join {{ ref('mrr_waterfall_monthly') }} as cur
        on cur.account_id = base.account_id
       and cur.month_start = base.month_start + interval 12 month
    where base.ending_mrr > 0
      and base.month_start + interval 12 month <= date '{{ var("analysis_end_date") }}'
    group by 1
)

select
    b.month_start,
    {{ date_key('b.month_start') }}                                                     as month_date_key,
    b.starting_mrr, b.new_mrr, b.reactivation_mrr, b.expansion_mrr,
    b.contraction_mrr, b.churned_mrr, b.ending_mrr,
    b.ending_mrr - b.starting_mrr                                                       as net_new_mrr,
    (b.starting_mrr + b.expansion_mrr - b.contraction_mrr - b.churned_mrr)
        / nullif(b.starting_mrr, 0)                                                     as nrr_mom,
    (b.starting_mrr - b.contraction_mrr - b.churned_mrr) / nullif(b.starting_mrr, 0)    as grr_mom,
    t.nrr_t12m,
    t.grr_t12m,
    b.churned_customers * 1.0 / nullif(b.starting_customers, 0)                         as logo_churn_rate,
    b.starting_customers, b.new_customers, b.reactivated_customers,
    b.churned_customers, b.ending_customers
from bridge as b
left join trailing_12m as t using (month_start)
order by b.month_start
