-- Weekly product usage per account (ISO weeks starting Monday).
-- Weekly active users are distinct users across the week, not a sum of daily counts.
with weekly as (
    select
        account_id,
        cast(date_trunc('week', session_date) as date)      as week_start,
        count(*)                                            as sessions,
        count(distinct user_id)                             as weekly_active_users,
        sum(duration_seconds) / 60.0                        as session_minutes,
        sum(dashboards_viewed)                              as dashboards_viewed,
        sum(reports_run)                                    as reports_run,
        sum(integrations_synced)                            as integrations_synced,
        sum(exports)                                        as exports,
        sum(api_calls)                                      as api_calls
    from {{ ref('stg_telemetry__sessions') }}
    group by 1, 2
)

select
    w.account_id || '-' || {{ date_key('w.week_start') }}   as usage_week_id,
    {{ date_key('w.week_start') }}                          as week_start_date_key,
    coalesce(d.customer_sk, -1)                             as customer_sk,
    w.account_id,
    w.week_start,
    cast(w.sessions as integer)                             as sessions,
    cast(w.weekly_active_users as integer)                  as weekly_active_users,
    cast(round(w.session_minutes, 1) as double)             as session_minutes,
    cast(w.dashboards_viewed as integer)                    as dashboards_viewed,
    cast(w.reports_run as integer)                          as reports_run,
    cast(w.integrations_synced as integer)                  as integrations_synced,
    cast(w.exports as integer)                              as exports,
    cast(w.api_calls as integer)                            as api_calls
from weekly as w
left join {{ ref('dim_customers') }} as d
    on d.account_id = w.account_id
   and w.week_start between d.valid_from and d.valid_to
