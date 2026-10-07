-- Map Stripe customers to CRM accounts. About 1% of Stripe customers are
-- missing the account_id metadata; those are matched on billing email domain.
with crm_domains as (
    select account_id, domain
    from {{ ref('stg_crm__account_changes') }}
    where change_type = 'created'
)

select
    c.stripe_customer_id,
    coalesce(c.metadata_account_id, d.account_id) as account_id,
    case when c.metadata_account_id is null then 'email_domain' else 'metadata' end as match_method
from {{ ref('stg_stripe__customers') }} as c
left join crm_domains as d
    on c.email_domain = d.domain
