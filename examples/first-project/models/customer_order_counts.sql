-- Hand-written model, registered with Loom in ./local/registrations
{{ config(materialized='table') }}

select
    hub.customer_id,
    count(orders.order_id) as order_count
from {{ ref('hub_customer') }} as hub
left join {{ ref('stg_orders') }} as orders
    on orders.customer_id = hub.customer_id
group by hub.customer_id
