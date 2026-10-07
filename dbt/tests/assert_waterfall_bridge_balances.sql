-- Every month: starting MRR + new + reactivation + expansion - contraction - churn = ending MRR,
-- and each month's ending MRR equals the next month's starting MRR.
with m as (
    select *, lead(starting_mrr) over (order by month_start) as next_starting_mrr
    from {{ ref('revenue_retention_monthly') }}
)

select *
from m
where abs(starting_mrr + new_mrr + reactivation_mrr + expansion_mrr
          - contraction_mrr - churned_mrr - ending_mrr) > 0.01
   or (next_starting_mrr is not null and abs(ending_mrr - next_starting_mrr) > 0.01)
