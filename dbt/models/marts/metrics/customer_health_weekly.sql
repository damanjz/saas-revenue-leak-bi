-- Composite customer health score, one row per paying account per week
-- (snapshot taken each Sunday and on the last day of data).
--
-- Four components, each scored 0-100. Weights were fixed up front, not fitted:
--   usage_trend  35%  sessions in the last 28 days vs the 28 days before
--   adoption     20%  average daily active users / paid seats (0.30 = full marks)
--   support      20%  tickets above the account's own 90-day baseline, high/urgent tickets
--   csat         25%  average CSAT over 90 days (no responses = neutral 75)
-- Bands: Healthy >= 70, Watch 50-69, At Risk < 50.
{{ config(materialized='table') }}

with daily as (
    select
        d.account_id,
        d.date_day,
        d.seats,
        d.mrr,
        coalesce(u.sessions, 0)         as sessions,
        coalesce(u.active_users, 0)     as active_users
    from {{ ref('int_account_mrr_daily') }} as d
    left join {{ ref('fact_product_usage') }} as u
        on u.account_id = d.account_id
       and u.usage_date = d.date_day
),

tickets_daily as (
    select
        account_id,
        cast(created_at as date)                                         as date_day,
        count(*)                                                         as tickets,
        count(*) filter (where priority in ('high', 'urgent'))           as high_tickets,
        sum(csat_score)                                                  as csat_sum,
        count(csat_score)                                                as csat_n
    from {{ ref('fact_support_tickets') }}
    where account_id <> 'UNKNOWN'
    group by 1, 2
),

joined as (
    select
        d.*,
        coalesce(t.tickets, 0)      as tickets,
        coalesce(t.high_tickets, 0) as high_tickets,
        coalesce(t.csat_sum, 0)     as csat_sum,
        coalesce(t.csat_n, 0)       as csat_n
    from daily as d
    left join tickets_daily as t using (account_id, date_day)
),

rolled as (
    select
        account_id,
        date_day,
        seats,
        mrr,
        sum(sessions)       over w28        as sessions_28d,
        sum(sessions)       over w_prev28   as sessions_prev_28d,
        avg(active_users)   over w28        as avg_daily_active_users_28d,
        sum(tickets)        over w28        as tickets_28d,
        sum(tickets)        over w_prev90   as tickets_prev_90d,
        sum(high_tickets)   over w28        as high_tickets_28d,
        sum(csat_sum)       over w90        as csat_sum_90d,
        sum(csat_n)         over w90        as csat_n_90d,
        count(*)            over w_tenure   as paid_days
    from joined
    window
        w28      as (partition by account_id order by date_day range between interval 27 days preceding and current row),
        w_prev28 as (partition by account_id order by date_day range between interval 55 days preceding and interval 28 days preceding),
        w90      as (partition by account_id order by date_day range between interval 89 days preceding and current row),
        w_prev90 as (partition by account_id order by date_day range between interval 117 days preceding and interval 28 days preceding),
        w_tenure as (partition by account_id order by date_day rows between unbounded preceding and current row)
),

snapshots as (
    select *
    from rolled
    where isodow(date_day) = 7
       or date_day = date '{{ var("analysis_end_date") }}'
),

scored as (
    select
        *,
        case when paid_days < 56 or coalesce(sessions_prev_28d, 0) = 0 then 75.0
             else {{ clamp_0_100('100.0 * sessions_28d / sessions_prev_28d') }} end            as usage_trend_score,
        {{ clamp_0_100('100.0 * (avg_daily_active_users_28d / nullif(seats, 0)) / 0.30') }}  as adoption_score,
        {{ clamp_0_100('100.0
            - 15.0 * greatest(0, tickets_28d - coalesce(tickets_prev_90d, 0) * 28.0 / 90.0 - 1)
            - 10.0 * high_tickets_28d') }}                                                    as support_score,
        case when csat_n_90d = 0 then 75.0
             else (csat_sum_90d * 1.0 / csat_n_90d - 1) / 4.0 * 100.0 end                     as csat_component_score
    from snapshots
)

select
    s.account_id || '-' || {{ date_key('s.date_day') }}                     as health_id,
    s.account_id,
    s.date_day                                                              as snapshot_date,
    {{ date_key('s.date_day') }}                                            as snapshot_date_key,
    coalesce(dc.customer_sk, -1)                                            as customer_sk,
    s.mrr,
    s.seats,
    s.sessions_28d,
    s.sessions_prev_28d,
    round(s.avg_daily_active_users_28d / nullif(s.seats, 0), 3)             as adoption_rate_28d,
    s.tickets_28d,
    s.high_tickets_28d,
    round(s.csat_sum_90d * 1.0 / nullif(s.csat_n_90d, 0), 2)                as csat_90d,
    round(s.usage_trend_score, 1)                                           as usage_trend_score,
    round(s.adoption_score, 1)                                              as adoption_score,
    round(s.support_score, 1)                                               as support_score,
    round(s.csat_component_score, 1)                                        as csat_component_score,
    round(0.35 * s.usage_trend_score + 0.20 * s.adoption_score
        + 0.20 * s.support_score + 0.25 * s.csat_component_score, 1)        as health_score,
    case
        when 0.35 * s.usage_trend_score + 0.20 * s.adoption_score
           + 0.20 * s.support_score + 0.25 * s.csat_component_score >= 70 then 'Healthy'
        when 0.35 * s.usage_trend_score + 0.20 * s.adoption_score
           + 0.20 * s.support_score + 0.25 * s.csat_component_score >= 50 then 'Watch'
        else 'At Risk'
    end                                                                     as health_band
from scored as s
left join {{ ref('dim_customers') }} as dc
    on dc.account_id = s.account_id
   and s.date_day between dc.valid_from and dc.valid_to
