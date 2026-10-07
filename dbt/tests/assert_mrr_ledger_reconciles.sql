-- The daily ledger must reproduce each account's MRR on the last day of data:
-- the sum of all movements equals the end-of-period subscription state.
with ledger as (
    select account_id, sum(mrr_delta) as ledger_mrr
    from {{ ref('fact_mrr_movements') }}
    group by 1
),

state as (
    select account_id, mrr as state_mrr
    from {{ ref('int_account_mrr_daily') }}
    where date_day = date '{{ var("analysis_end_date") }}'
)

select *
from ledger
full outer join state using (account_id)
where abs(coalesce(ledger_mrr, 0) - coalesce(state_mrr, 0)) > 0.005
