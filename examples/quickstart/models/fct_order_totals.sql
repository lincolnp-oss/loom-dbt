-- Hand-written model, registered with Loom in ./local/registrations
{{ config(materialized='table') }}

select
    order_id,
    sum(quantity) as total_quantity
from {{ ref('stg_order_items') }}
group by order_id
