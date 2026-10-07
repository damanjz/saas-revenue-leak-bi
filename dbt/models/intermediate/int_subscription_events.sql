-- Subscription lifecycle events with webhook duplicates removed. A duplicate
-- is the same subscription change (same type, time, seats and MRR) delivered
-- again under a different event_id.
with events as (
    select e.*, m.account_id
    from {{ ref('stg_stripe__events') }} as e
    left join {{ ref('int_stripe_customer_accounts') }} as m using (stripe_customer_id)
    where e.event_type like 'customer.subscription.%'
)

select
    *,
    count(*) over (
        partition by subscription_id, event_type, occurred_at, quantity, mrr_cents, previous_mrr_cents
    ) - 1 as duplicate_deliveries_removed
from events
qualify row_number() over (
    partition by subscription_id, event_type, occurred_at, quantity, mrr_cents, previous_mrr_cents
    order by event_id
) = 1
