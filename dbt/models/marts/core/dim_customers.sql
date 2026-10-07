-- SCD Type 2 customer dimension. A new version opens whenever the account
-- manager, CS tier, company size band or ARR band changes. Row -1 is the
-- unknown member for facts whose account cannot be resolved.
with versions as (
    select * from {{ ref('int_customer_versions') }}
),

first_revenue as (
    select account_id, min(date_day) as first_subscription_date
    from {{ ref('int_account_mrr_daily') }}
    group by 1
),

scd as (
    select
        cast(row_number() over (order by v.account_id, v.effective_date) as integer) as customer_sk,
        v.account_id,
        v.effective_date                                                as valid_from,
        coalesce(
            cast(lead(v.effective_date) over w - interval 1 day as date),
            date '9999-12-31'
        )                                                               as valid_to,
        lead(v.effective_date) over w is null                           as is_current,
        v.company_name, v.domain, v.industry, v.region, v.segment,
        v.employee_count, v.company_size_band, v.cs_tier, v.acquisition_channel,
        v.account_manager, v.arr_band,
        f.first_subscription_date,
        cast(date_trunc('month', f.first_subscription_date) as date)    as cohort_month
    from versions as v
    left join first_revenue as f using (account_id)
    window w as (partition by v.account_id order by v.effective_date)
)

select
    customer_sk, account_id, valid_from, valid_to, is_current,
    company_name, domain, industry, region, segment, employee_count,
    company_size_band, cs_tier, acquisition_channel, account_manager, arr_band,
    first_subscription_date, cohort_month
from scd

union all

select
    -1, 'UNKNOWN', date '1900-01-01', date '9999-12-31', true,
    'Unknown', null, 'Unknown', 'Unknown', 'Unknown', null,
    'Unknown', 'Unknown', 'Unknown', 'Unassigned', 'Unknown',
    null, null
