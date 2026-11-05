-- Singular reconciliation test (practitioner-authored): the
-- hand-written fct_order_totals must agree with the staged order
-- lines for every order. Returns one row per disagreement; dbt
-- fails the test when any row is returned.
WITH staged AS (
    SELECT order_id, SUM(quantity) AS total_quantity
    FROM {{ ref('stg_order_items') }}
    GROUP BY order_id
),
totals AS (
    SELECT order_id, total_quantity
    FROM {{ ref('fct_order_totals') }}
)
SELECT COALESCE(staged.order_id, totals.order_id) AS order_id
FROM staged
FULL OUTER JOIN totals ON totals.order_id = staged.order_id
WHERE staged.order_id IS NULL
   OR totals.order_id IS NULL
   OR staged.total_quantity <> totals.total_quantity
