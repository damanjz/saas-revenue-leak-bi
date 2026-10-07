-- End-of-day MRR state per account on every day it changed.
with last_event_of_day as (
    select
        account_id,
        event_date,
        mrr_cents,
        quantity,
        plan_id,
        billing_interval,
        change_reason
    from {{ ref('int_subscription_events') }}
    qualify row_number() over (
        partition by account_id, event_date
        order by occurred_at desc, event_id desc
    ) = 1
)

select
    *,
    lag(mrr_cents, 1, 0) over w as previous_mrr_cents,
    lag(quantity) over w        as previous_quantity,
    lag(plan_id) over w         as previous_plan_id,
    coalesce(sum(case when mrr_cents > 0 then 1 else 0 end) over (
        partition by account_id order by event_date
        rows between unbounded preceding and 1 preceding
    ), 0) > 0                   as had_prior_revenue,
    lead(event_date) over w     as next_change_date
from last_event_of_day
window w as (partition by account_id order by event_date)
