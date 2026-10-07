{# Parse an ISO-8601 string with any offset (Z, +05:30, ...) into a naive UTC timestamp. #}
{% macro to_utc(col) -%}
    cast(timezone('UTC', cast({{ col }} as timestamptz)) as timestamp)
{%- endmacro %}

{# Cast a CSV value that pandas may have written as a float ("12.0") to an integer type. #}
{% macro to_int(col, type='integer') -%}
    try_cast(try_cast(nullif(trim({{ col }}), '') as double) as {{ type }})
{%- endmacro %}

{% macro date_key(col) -%}
    cast(strftime({{ col }}, '%Y%m%d') as integer)
{%- endmacro %}

{# Clamp an expression to the 0..100 scoring range. #}
{% macro clamp_0_100(expr) -%}
    least(100.0, greatest(0.0, {{ expr }}))
{%- endmacro %}

{% macro arr_band(mrr_cents) -%}
    case
        when {{ mrr_cents }} <= 0 then 'Churned'
        when {{ mrr_cents }} * 12 < 1000000 then 'Under $10K'
        when {{ mrr_cents }} * 12 < 5000000 then '$10K-$50K'
        when {{ mrr_cents }} * 12 < 10000000 then '$50K-$100K'
        else '$100K+'
    end
{%- endmacro %}
