-- Change points for the SCD2 customer dimension: every CRM change (AM, tier,
-- company size) plus every ARR band change derived from billing. Versions use
-- effective dates, so CRM records that arrived weeks late still land in the
-- right place in history.
with crm_daily as (
    select
        account_id,
        effective_date,
        max(recorded_date)          as recorded_date,
        max(company_name)           as company_name,
        max(domain)                 as domain,
        max(industry)               as industry,
        max(region)                 as region,
        max(segment)                as segment,
        max(employee_count)         as employee_count,
        max(company_size_band)      as company_size_band,
        max(cs_tier)                as cs_tier,
        max(acquisition_channel)    as acquisition_channel,
        max(account_manager)        as account_manager
    from {{ ref('stg_crm__account_changes') }}
    group by 1, 2
),

arr_band_changes as (
    select account_id, event_date as effective_date, arr_band
    from (
        select
            account_id,
            event_date,
            {{ arr_band('mrr_cents') }} as arr_band,
            lag({{ arr_band('mrr_cents') }}) over (partition by account_id order by event_date) as prev_band
        from {{ ref('int_account_mrr_changes') }}
    )
    where prev_band is null or arr_band <> prev_band
),

change_points as (
    select account_id, effective_date from crm_daily
    union
    select account_id, effective_date from arr_band_changes
),

joined as (
    select
        p.account_id,
        p.effective_date,
        c.recorded_date,
        c.company_name, c.domain, c.industry, c.region, c.segment, c.employee_count,
        c.company_size_band, c.cs_tier, c.acquisition_channel, c.account_manager,
        a.arr_band
    from change_points as p
    left join crm_daily as c using (account_id, effective_date)
    left join arr_band_changes as a using (account_id, effective_date)
)

select
    account_id,
    effective_date,
    max(recorded_date) over w_all                                   as last_recorded_date,
    last_value(company_name ignore nulls) over w                    as company_name,
    last_value(domain ignore nulls) over w                          as domain,
    last_value(industry ignore nulls) over w                        as industry,
    last_value(region ignore nulls) over w                          as region,
    last_value(segment ignore nulls) over w                         as segment,
    last_value(employee_count ignore nulls) over w                  as employee_count,
    last_value(company_size_band ignore nulls) over w               as company_size_band,
    last_value(cs_tier ignore nulls) over w                         as cs_tier,
    last_value(acquisition_channel ignore nulls) over w             as acquisition_channel,
    last_value(account_manager ignore nulls) over w                 as account_manager,
    coalesce(last_value(arr_band ignore nulls) over w, 'Pre-revenue') as arr_band
from joined
window
    w as (partition by account_id order by effective_date rows between unbounded preceding and current row),
    w_all as (partition by account_id)
