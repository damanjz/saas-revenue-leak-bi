-- Timestamps arrive in mixed offsets (Z and +05:30); all are normalised to UTC.
-- Priority casing is normalised. Out-of-range CSAT and impossible resolution
-- times are kept here (so tests can flag them) and nulled in the fact table.
select
    cast(ticket_id as integer)                              as ticket_id,
    nullif(trim(organization_external_id), '')              as account_id,
    lower(requester_email)                                  as requester_email,
    {{ to_utc('created_at') }}                              as created_at,
    {{ to_utc('first_response_at') }}                       as first_response_at,
    {{ to_utc('solved_at') }}                               as solved_at,
    lower(trim(status))                                     as status,
    lower(trim(priority))                                   as priority,
    lower(trim(channel))                                    as channel,
    lower(trim(category))                                   as category,
    tags,
    {{ to_int('csat_score') }}                              as csat_score_raw
from {{ source('zendesk', 'zendesk_tickets') }}
