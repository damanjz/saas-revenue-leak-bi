{{ config(materialized='table') }}

-- Exact duplicate rows (same session_id) are collapsed. Durations that are
-- missing or negative are set to null and flagged.
with source as (
    select * from {{ source('telemetry', 'telemetry_sessions') }}
),

typed as (
    select
        session_id,
        account_id,
        user_id,
        cast(session_start as timestamp)               as session_start,
        try_cast(nullif(session_end, '') as timestamp) as session_end,
        cast(dashboards_viewed as integer)             as dashboards_viewed,
        cast(reports_run as integer)                   as reports_run,
        cast(integrations_synced as integer)           as integrations_synced,
        cast(exports as integer)                       as exports,
        cast(api_calls as integer)                     as api_calls
    from source
    qualify row_number() over (partition by session_id order by session_start) = 1
)

select
    *,
    cast(session_start as date) as session_date,
    case
        when session_end is null or session_end < session_start then null
        else date_diff('second', session_start, session_end)
    end                         as duration_seconds,
    session_end is null         as is_missing_end,
    session_end < session_start as is_negative_duration
from typed
