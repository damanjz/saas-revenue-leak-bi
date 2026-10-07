-- Daily product usage per account. Grain: account_id x date (active days only).
select
    u.account_id || '-' || {{ date_key('u.session_date') }}     as usage_id,
    {{ date_key('u.session_date') }}                            as date_key,
    coalesce(d.customer_sk, -1)                                 as customer_sk,
    u.account_id,
    u.session_date                                              as usage_date,
    cast(count(*) as integer)                                   as sessions,
    cast(count(distinct u.user_id) as integer)                  as active_users,
    cast(round(sum(u.duration_seconds) / 60.0, 1) as double)    as session_minutes,
    cast(sum(case when u.duration_seconds is null then 1 else 0 end) as integer) as sessions_missing_duration,
    cast(sum(u.dashboards_viewed) as integer)                   as dashboards_viewed,
    cast(sum(u.reports_run) as integer)                         as reports_run,
    cast(sum(u.integrations_synced) as integer)                 as integrations_synced,
    cast(sum(u.exports) as integer)                             as exports,
    cast(sum(u.api_calls) as integer)                           as api_calls
from {{ ref('stg_telemetry__sessions') }} as u
left join {{ ref('dim_customers') }} as d
    on d.account_id = u.account_id
   and u.session_date between d.valid_from and d.valid_to
group by u.account_id, u.session_date, d.customer_sk
