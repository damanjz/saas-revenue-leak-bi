-- One row per support ticket with SLA measures. Invalid source values (CSAT
-- outside 1-5, solved before created) are nulled and flagged, not dropped.
with measured as (
    select
        t.*,
        date_diff('minute', t.created_at, t.first_response_at)              as first_response_minutes,
        case when t.solved_at >= t.created_at
             then date_diff('minute', t.created_at, t.solved_at) / 60.0 end  as resolution_hours,
        coalesce(t.solved_at < t.created_at, false)                          as is_invalid_resolution,
        coalesce(t.csat_score_raw not between 1 and 5, false)                as is_invalid_csat
    from {{ ref('stg_zendesk__tickets') }} as t
)

select
    m.ticket_id,
    {{ date_key('cast(m.created_at as date)') }}                    as created_date_key,
    case when m.solved_at is not null and not m.is_invalid_resolution
         then {{ date_key('cast(m.solved_at as date)') }} end       as solved_date_key,
    coalesce(d.customer_sk, -1)                                     as customer_sk,
    coalesce(m.account_id, 'UNKNOWN')                               as account_id,
    m.created_at,
    case when m.is_invalid_resolution then null else m.solved_at end as solved_at,
    m.status,
    m.priority,
    m.channel,
    m.category,
    m.tags,
    coalesce(m.tags like '%bug%', false)                            as is_bug,
    cast(m.first_response_minutes as integer)                       as first_response_minutes,
    cast(round(m.resolution_hours, 2) as double)                    as resolution_hours,
    s.first_response_target_minutes,
    s.resolution_target_hours,
    m.first_response_minutes > s.first_response_target_minutes      as first_response_sla_breached,
    m.resolution_hours > s.resolution_target_hours                  as resolution_sla_breached,
    case when m.is_invalid_csat then null else m.csat_score_raw end as csat_score,
    m.is_invalid_csat,
    m.is_invalid_resolution
from measured as m
left join {{ ref('sla_policy') }} as s
    on s.priority = m.priority
left join {{ ref('dim_customers') }} as d
    on d.account_id = m.account_id
   and cast(m.created_at as date) between d.valid_from and d.valid_to
