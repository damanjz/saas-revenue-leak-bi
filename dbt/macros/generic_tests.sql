{# Values must fall inside [min_value, max_value]; nulls are ignored. #}
{% test accepted_range(model, column_name, min_value=none, max_value=none) %}
select *
from {{ model }}
where {{ column_name }} is not null
  and (
    {% if min_value is not none %} {{ column_name }} < {{ min_value }} {% else %} false {% endif %}
    or
    {% if max_value is not none %} {{ column_name }} > {{ max_value }} {% else %} false {% endif %}
  )
{% endtest %}

{# The combination of columns must be unique. #}
{% test unique_combination_of_columns(model, combination_of_columns) %}
select {{ combination_of_columns | join(', ') }}, count(*) as n_rows
from {{ model }}
group by {{ combination_of_columns | join(', ') }}
having count(*) > 1
{% endtest %}

{# SCD Type 2: versions of the same key must not overlap and must not leave gaps. #}
{% test scd2_valid_ranges(model, key, valid_from='valid_from', valid_to='valid_to') %}
with ordered as (
    select
        {{ key }} as k,
        {{ valid_from }} as vf,
        {{ valid_to }} as vt,
        lead({{ valid_from }}) over (partition by {{ key }} order by {{ valid_from }}) as next_vf
    from {{ model }}
)
select *
from ordered
where vt < vf
   or (next_vf is not null and next_vf <> vt + interval 1 day)
{% endtest %}

{# Exactly one current row per key. #}
{% test one_current_row(model, key, flag='is_current') %}
select {{ key }}, count(*) as current_rows
from {{ model }}
where {{ flag }}
group by {{ key }}
having count(*) <> 1
{% endtest %}

{# Every row must satisfy a boolean SQL expression. #}
{% test expression_is_true(model, expression, column_name=none) %}
select *
from {{ model }}
where not ({{ expression }})
{% endtest %}
