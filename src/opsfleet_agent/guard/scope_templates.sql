-- Brand-scope templates (spec 30, B2.3). The guard replaces each table reference of the model's
-- SQL with one of these subqueries. `{dataset}` is the full dataset name from config/settings.yaml.
-- `@scope_brands` is an array parameter with the exact brand names of the manager. No brand name is
-- ever written into the SQL text.
-- A table without a template for a role stays unchanged. The users template removes the PII columns
-- for all roles.

-- template: manager.order_items
SELECT x.id, x.order_id, x.user_id, x.product_id, x.inventory_item_id, x.status,
       x.created_at, x.shipped_at, x.delivered_at, x.returned_at, x.sale_price
FROM `{dataset}.order_items` AS x
JOIN `{dataset}.products` AS p ON p.id = x.product_id
WHERE p.brand IN UNNEST(@scope_brands)

-- template: manager.products
SELECT x.id, x.cost, x.category, x.name, x.brand, x.retail_price, x.department, x.sku,
       x.distribution_center_id
FROM `{dataset}.products` AS x
WHERE x.brand IN UNNEST(@scope_brands)

-- template: manager.orders
SELECT x.order_id, x.user_id, x.status, x.gender, x.created_at, x.returned_at, x.shipped_at,
       x.delivered_at
FROM `{dataset}.orders` AS x
WHERE x.order_id IN (
  SELECT i.order_id
  FROM `{dataset}.order_items` AS i
  JOIN `{dataset}.products` AS p ON p.id = i.product_id
  WHERE p.brand IN UNNEST(@scope_brands))

-- template: manager.users
SELECT x.id, x.age, x.gender, x.state, x.city, x.country, x.traffic_source, x.created_at
FROM `{dataset}.users` AS x
WHERE x.id IN (
  SELECT i.user_id
  FROM `{dataset}.order_items` AS i
  JOIN `{dataset}.products` AS p ON p.id = i.product_id
  WHERE p.brand IN UNNEST(@scope_brands))

-- template: ceo.users
SELECT x.id, x.age, x.gender, x.state, x.city, x.country, x.traffic_source, x.created_at
FROM `{dataset}.users` AS x
