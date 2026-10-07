{{ config(materialized='table') }}

-- One row per account per day with positive MRR (dense daily revenue state).
select
    c.account_id,
    s.date_day,
    c.mrr_cents,
    c.mrr_cents / 100.0 as mrr,
    c.quantity          as seats,
    c.plan_id,
    c.billing_interval
from {{ ref('int_account_mrr_changes') }} as c
join {{ ref('util_date_spine') }} as s
    on s.date_day >= c.event_date
   and s.date_day < coalesce(c.next_change_date, date '{{ var("analysis_end_date") }}' + interval 1 day)
where c.mrr_cents > 0
