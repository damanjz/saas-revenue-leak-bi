-- One row per Stripe webhook event. Exact re-deliveries (same event_id) are
-- dropped here; duplicates that arrive with a NEW event_id are kept so the
-- data tests can see them, and are removed in int_subscription_events.
with source as (
    select * from {{ source('stripe', 'stripe_events') }}
)

select
    event_id,
    event_type,
    {{ to_utc('occurred_at') }}                     as occurred_at,
    cast({{ to_utc('occurred_at') }} as date)       as event_date,
    customer                                        as stripe_customer_id,
    subscription_id,
    lower(trim(plan_id))                            as plan_id,
    billing_interval,
    {{ to_int('quantity') }}                        as quantity,
    {{ to_int('unit_amount_cents', 'bigint') }}     as unit_amount_cents,
    {{ to_int('mrr_cents', 'bigint') }}             as mrr_cents,
    lower(trim(previous_plan_id))                   as previous_plan_id,
    {{ to_int('previous_quantity') }}               as previous_quantity,
    {{ to_int('previous_mrr_cents', 'bigint') }}    as previous_mrr_cents,
    change_reason,
    {{ to_int('amount_paid_cents', 'bigint') }}     as amount_paid_cents
from source
qualify row_number() over (partition by event_id order by occurred_at) = 1
