-- Daily MRR ledger: one row per account per day on which its MRR changed.
-- Grain: account_id x date. Summing mrr_delta over all time gives current MRR.
with changes as (
    select * from {{ ref('int_account_mrr_changes') }}
    where mrr_cents <> previous_mrr_cents
)

select
    c.account_id || '-' || {{ date_key('c.event_date') }}       as movement_id,
    {{ date_key('c.event_date') }}                              as date_key,
    coalesce(d.customer_sk, -1)                                 as customer_sk,
    c.account_id,
    c.event_date                                                as movement_date,
    case
        when c.previous_mrr_cents = 0 and not c.had_prior_revenue then 'New'
        when c.previous_mrr_cents = 0                              then 'Reactivation'
        when c.mrr_cents = 0                                       then 'Churn'
        when c.mrr_cents > c.previous_mrr_cents                    then 'Expansion'
        else 'Contraction'
    end                                                         as movement_type,
    coalesce(c.change_reason, 'unknown')                        as change_reason,
    c.plan_id,
    c.previous_plan_id,
    c.quantity                                                  as seats,
    c.previous_quantity                                         as previous_seats,
    cast(c.previous_mrr_cents / 100.0 as decimal(12, 2))        as mrr_before,
    cast(c.mrr_cents / 100.0 as decimal(12, 2))                 as mrr_after,
    cast((c.mrr_cents - c.previous_mrr_cents) / 100.0 as decimal(12, 2)) as mrr_delta
from changes as c
left join {{ ref('dim_customers') }} as d
    on d.account_id = c.account_id
   and c.event_date between d.valid_from and d.valid_to
