select
    customer                                    as stripe_customer_id,
    lower(email)                                as billing_email,
    lower(split_part(email, '@', 2))            as email_domain,
    nullif(trim(metadata_account_id), '')       as metadata_account_id,
    {{ to_utc('created') }}                     as created_at
from {{ source('stripe', 'stripe_customers') }}
