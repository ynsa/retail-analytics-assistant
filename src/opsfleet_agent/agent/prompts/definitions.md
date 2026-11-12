<!-- Metrics glossary (spec 31, B2.6; gap analysis N4). The agent, the answer footer and the
     reference SQL of the evaluation (spec 21) use these definitions. Each entry has a key heading
     and the lines Meaning, Footer and SQL. Excludes names the statuses that the SQL must leave
     out: code checks it before the query runs. Code parses this format. -->
# Metrics glossary

`oi` is `order_items`, `p` is `products`.

### revenue
- **Meaning:** The sale price of order items that were not cancelled or returned.
- **Footer:** Revenue = sale price of items that were not cancelled or returned.
- **Excludes:** Cancelled, Returned
- **SQL:** `SUM(oi.sale_price)` with `oi.status NOT IN ('Cancelled', 'Returned')`

### sale_date
- **Meaning:** The date of a sale is the creation time of the order item, in UTC. All period metrics use it.
- **Footer:** Sale date = creation date of the order item (UTC).
- **SQL:** `oi.created_at`

### today
- **Meaning:** The current UTC date. The data has some dates after today. Period metrics leave out these rows.
- **Footer:** Rows dated after today are left out.
- **SQL:** `oi.created_at < TIMESTAMP('<tomorrow>')`

### purchase
- **Meaning:** An order item that was not cancelled.
- **Footer:** Purchase = order item that was not cancelled.
- **Excludes:** Cancelled
- **SQL:** `oi.status != 'Cancelled'`

### customer_of_brand
- **Meaning:** A customer with at least one purchase of the brand in the period.
- **Footer:** Customer of a brand = at least one purchase of the brand in the period.
- **Excludes:** Cancelled
- **SQL:** `COUNT(DISTINCT oi.user_id)` over purchases joined to `p.brand`

### gross_margin
- **Meaning:** Sale price minus product cost, for revenue items.
- **Footer:** Gross margin = sale price minus product cost, for items that were not cancelled or returned.
- **Excludes:** Cancelled, Returned
- **SQL:** `SUM(oi.sale_price - p.cost)` with the revenue filter

### repeat_customer
- **Meaning:** A customer with purchases in 2 or more distinct orders in the period ("bought again"). For a manager, only the orders with the user's brands count.
- **Footer:** Repeat customer = purchases in 2 or more orders in the period.
- **Excludes:** Cancelled
- **SQL:** `COUNT(DISTINCT oi.order_id) >= 2` for each `oi.user_id`, over purchases (`oi.status != 'Cancelled'`) in the period. The repeat rate is the repeat customers / the customers with a purchase.

### churned_customer
- **Meaning:** A customer with at least one purchase and no purchase in the last 90 days.
- **Footer:** Churned customer = no purchase in the last 90 days.
- **Excludes:** Cancelled
- **SQL:** `MAX(oi.created_at) < TIMESTAMP_SUB(<today>, INTERVAL 90 DAY)` for each `oi.user_id` with a purchase

### monthly_churn_rate
- **Meaning:** Customers who reached 90 days without a purchase in that month, divided by the active customers at the start of the month.
- **Footer:** Monthly churn rate = customers who reached 90 days without a purchase in the month / active customers at the start of the month.
- **Excludes:** Cancelled
- **SQL:** customers whose churn day (the day 90 days after a purchase with no next purchase in these 90 days) falls in the month, divided by the customers with a purchase in the 90 days before the month start. Use this pattern. Change only the dates: the end of the data (`<end>`, the day after the last month), the first month and the last month.

```sql
WITH purchases AS (
  SELECT DISTINCT oi.user_id, DATE(oi.created_at) AS d
  FROM `bigquery-public-data.thelook_ecommerce.order_items` AS oi
  WHERE oi.status != 'Cancelled' AND oi.created_at < TIMESTAMP('<end>')
),
gaps AS (
  SELECT user_id, d, LEAD(d) OVER (PARTITION BY user_id ORDER BY d) AS next_d
  FROM purchases
),
churn AS (
  SELECT user_id, DATE_ADD(d, INTERVAL 90 DAY) AS churn_d
  FROM gaps
  WHERE next_d IS NULL OR next_d > DATE_ADD(d, INTERVAL 90 DAY)
),
months AS (
  SELECT m FROM UNNEST(GENERATE_DATE_ARRAY(DATE '<first month>', DATE '<last month>', INTERVAL 1 MONTH)) AS m
),
active AS (
  SELECT months.m, COUNT(DISTINCT purchases.user_id) AS active_customers
  FROM months CROSS JOIN purchases
  WHERE purchases.d >= DATE_SUB(months.m, INTERVAL 90 DAY) AND purchases.d < months.m
  GROUP BY months.m
),
churned AS (
  SELECT DATE_TRUNC(churn_d, MONTH) AS m, COUNT(DISTINCT user_id) AS churned_customers
  FROM churn
  WHERE churn_d >= DATE '<first month>' AND churn_d < DATE '<end>'
  GROUP BY m
)
SELECT FORMAT_DATE('%Y-%m', active.m) AS month, churned.churned_customers,
  active.active_customers,
  SAFE_DIVIDE(churned.churned_customers, active.active_customers) * 100 AS churn_rate_pct
FROM active LEFT JOIN churned ON churned.m = active.m
ORDER BY month
```

### underspending
- **Meaning:** Average spend per customer below the company average or below the compared group.
- **Footer:** Underspending = spend per customer below the compared group.
- **SQL:** compare `spend_per_customer` of the group with the company or the other group

### quarter
- **Meaning:** A calendar quarter: Q1 is January to March.
- **Footer:** Quarters are calendar quarters.
- **SQL:** `EXTRACT(QUARTER FROM oi.created_at)` and the year

### last_month
- **Meaning:** The last full calendar month before today.
- **Footer:** Last month = the last full calendar month.
- **SQL:** the `last_month` dates of the calendar

### average_order_value
- **Meaning:** Revenue divided by the number of distinct orders with revenue.
- **Footer:** Average order value = revenue / orders.
- **Excludes:** Cancelled, Returned
- **SQL:** `SUM(oi.sale_price) / COUNT(DISTINCT oi.order_id)` with the revenue filter

### spend_per_customer
- **Meaning:** Revenue divided by the number of distinct customers with revenue.
- **Footer:** Spend per customer = revenue / customers with revenue.
- **Excludes:** Cancelled, Returned
- **SQL:** `SUM(oi.sale_price) / COUNT(DISTINCT oi.user_id)` with the revenue filter

### return_rate
- **Meaning:** Items with status Returned divided by items with a status other than Cancelled.
- **Footer:** Return rate = returned items / items that were not cancelled.
- **Excludes:** Cancelled
- **SQL:** `COUNTIF(oi.status = 'Returned') / COUNTIF(oi.status != 'Cancelled')`

### cancellation_rate
- **Meaning:** Items with status Cancelled divided by all items.
- **Footer:** Cancellation rate = cancelled items / all items.
- **SQL:** `COUNTIF(oi.status = 'Cancelled') / COUNT(*)`

### orders
- **Meaning:** Distinct orders with at least one revenue item. It uses the same filter as revenue, so the figures of one answer agree.
- **Footer:** Orders = orders with at least one item that was not cancelled or returned.
- **Excludes:** Cancelled, Returned
- **SQL:** `COUNT(DISTINCT oi.order_id)` with the revenue filter

### items_sold
- **Meaning:** Order items that were not cancelled or returned.
- **Footer:** Items sold = items that were not cancelled or returned.
- **Excludes:** Cancelled, Returned
- **SQL:** `COUNT(*)` of order items with the revenue filter
