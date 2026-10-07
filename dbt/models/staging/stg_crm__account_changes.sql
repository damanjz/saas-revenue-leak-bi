select
    account_id,
    cast(effective_at as date)                                  as effective_date,
    cast(recorded_at as date)                                   as recorded_date,
    date_diff('day', cast(effective_at as date), cast(recorded_at as date)) as recording_lag_days,
    change_type,
    nullif(company_name, '')        as company_name,
    nullif(domain, '')              as domain,
    nullif(industry, '')            as industry,
    nullif(region, '')              as region,
    nullif(segment, '')             as segment,
    {{ to_int('employee_count') }}  as employee_count,
    nullif(company_size_band, '')   as company_size_band,
    nullif(cs_tier, '')             as cs_tier,
    nullif(acquisition_channel, '') as acquisition_channel,
    nullif(account_manager, '')     as account_manager
from {{ source('crm', 'crm_account_changes') }}
