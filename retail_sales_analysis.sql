-- =====================================================================
-- Retail Sales Analysis: Customer Segmentation, Sales Trends, Basket Analysis
-- Target: SQLite
--
-- HOW TO RUN:
--   Keep this file in the same folder as retail_sales_clean.csv, then:
--     sqlite3 retail.db < retail_sales_analysis.sql
--   This will create retail.db, load the cleaned data, and print results
--   for every analytical query below.
-- =====================================================================

.mode csv
.headers on

-- =====================================================================
-- SECTION 1: TABLE SCHEMA
-- =====================================================================

DROP TABLE IF EXISTS sales;

CREATE TABLE sales (
    invoice_no   TEXT    NOT NULL,
    stock_code   TEXT    NOT NULL,
    description  TEXT,
    quantity     INTEGER NOT NULL,
    invoice_date TEXT    NOT NULL,   -- ISO 8601: YYYY-MM-DD HH:MM:SS
    unit_price   REAL    NOT NULL,
    customer_id  INTEGER NOT NULL,
    country      TEXT    NOT NULL,
    total_price  REAL    NOT NULL
);

-- =====================================================================
-- SECTION 2: SEED DATA
-- Loaded directly from the cleaned CSV (single source of truth shared
-- with the Python script and dashboard) via SQLite's CSV importer.
-- =====================================================================

.import --skip 1 retail_sales_clean.csv sales

CREATE INDEX idx_sales_customer  ON sales(customer_id);
CREATE INDEX idx_sales_invoice   ON sales(invoice_no);
CREATE INDEX idx_sales_date      ON sales(invoice_date);
CREATE INDEX idx_sales_stockcode ON sales(stock_code);

-- =====================================================================
-- SECTION 3: ANALYTICAL QUERIES
-- =====================================================================

.print ''
.print '--- 3.1 Overview metrics ---'
SELECT
    COUNT(*)                              AS line_items,
    COUNT(DISTINCT invoice_no)            AS orders,
    COUNT(DISTINCT customer_id)           AS customers,
    COUNT(DISTINCT stock_code)            AS products,
    ROUND(SUM(total_price), 2)            AS total_revenue,
    ROUND(SUM(total_price) / COUNT(DISTINCT invoice_no), 2) AS avg_order_value
FROM sales;

-- 3.2 Customer RFM base table (recency in days from day after last transaction in dataset)
DROP VIEW IF EXISTS customer_rfm;
CREATE VIEW customer_rfm AS
WITH snapshot AS (
    SELECT DATE(MAX(invoice_date), '+1 day') AS snapshot_date FROM sales
)
SELECT
    s.customer_id,
    CAST(JULIANDAY((SELECT snapshot_date FROM snapshot)) - JULIANDAY(MAX(s.invoice_date)) AS INTEGER) AS recency_days,
    COUNT(DISTINCT s.invoice_no) AS frequency,
    ROUND(SUM(s.total_price), 2) AS monetary
FROM sales s
GROUP BY s.customer_id;

-- 3.3 Customer segments via RFM quartile scoring (mirrors Python segmentation logic)
-- NOTE: SQLite's NTILE() and pandas' qcut() can break ties between customers
-- who share an identical recency/frequency/monetary value differently (each
-- engine orders tied rows differently before splitting into quartiles). This
-- can shift a handful of customers between adjacent segments here vs. the
-- Python output, but totals (overall revenue, customer count) match exactly,
-- and the six segments and their relative size/value ranking are the same.
DROP VIEW IF EXISTS customer_segments;
CREATE VIEW customer_segments AS
WITH scored AS (
    SELECT
        customer_id, recency_days, frequency, monetary,
        NTILE(4) OVER (ORDER BY recency_days DESC) AS r_score,
        NTILE(4) OVER (ORDER BY frequency ASC)     AS f_score,
        NTILE(4) OVER (ORDER BY monetary ASC)      AS m_score
    FROM customer_rfm
)
SELECT
    *,
    CASE
        WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'Champions'
        WHEN r_score >= 3 AND f_score >= 3 THEN 'Loyal Customers'
        WHEN r_score >= 4 AND f_score <= 2 THEN 'New Customers'
        WHEN r_score <= 2 AND f_score >= 3 AND m_score >= 3 THEN 'At Risk'
        WHEN r_score <= 2 AND f_score <= 2 AND m_score <= 2 THEN 'Hibernating'
        ELSE 'Needs Attention'
    END AS segment
FROM scored;

.print ''
.print '--- 3.4 Segment-level summary ---'
SELECT
    segment,
    COUNT(*)                         AS customers,
    ROUND(AVG(recency_days), 2)      AS avg_recency_days,
    ROUND(AVG(frequency), 2)         AS avg_frequency,
    ROUND(AVG(monetary), 2)          AS avg_monetary,
    ROUND(SUM(monetary), 2)          AS total_revenue
FROM customer_segments
GROUP BY segment
ORDER BY total_revenue DESC;

.print ''
.print '--- 3.5 Top 10 customers by total spend ---'
SELECT customer_id, recency_days, frequency, monetary, segment
FROM customer_segments
ORDER BY monetary DESC
LIMIT 10;

.print ''
.print '--- 3.6 Monthly sales trend ---'
SELECT
    strftime('%Y-%m', invoice_date)                       AS year_month,
    ROUND(SUM(total_price), 2)                            AS revenue,
    COUNT(DISTINCT invoice_no)                            AS orders,
    COUNT(DISTINCT customer_id)                           AS customers,
    ROUND(SUM(total_price) / COUNT(DISTINCT invoice_no), 2) AS avg_order_value
FROM sales
GROUP BY year_month
ORDER BY year_month;

.print ''
.print '--- 3.7 Revenue by day of week ---'
SELECT
    CASE CAST(strftime('%w', invoice_date) AS INTEGER)
        WHEN 0 THEN 'Sunday' WHEN 1 THEN 'Monday' WHEN 2 THEN 'Tuesday'
        WHEN 3 THEN 'Wednesday' WHEN 4 THEN 'Thursday' WHEN 5 THEN 'Friday'
        WHEN 6 THEN 'Saturday' END AS day_of_week,
    ROUND(SUM(total_price), 2) AS revenue,
    COUNT(DISTINCT invoice_no) AS orders
FROM sales
GROUP BY day_of_week
ORDER BY MIN(strftime('%w', invoice_date));

.print ''
.print '--- 3.8 Revenue by hour of day ---'
SELECT
    CAST(strftime('%H', invoice_date) AS INTEGER) AS hour_of_day,
    ROUND(SUM(total_price), 2) AS revenue,
    COUNT(DISTINCT invoice_no) AS orders
FROM sales
GROUP BY hour_of_day
ORDER BY hour_of_day;

.print ''
.print '--- 3.9 Top 10 countries by revenue ---'
SELECT
    country,
    ROUND(SUM(total_price), 2) AS revenue,
    COUNT(DISTINCT customer_id) AS customers
FROM sales
GROUP BY country
ORDER BY revenue DESC
LIMIT 10;

.print ''
.print '--- 3.10 Top 10 products by revenue ---'
SELECT
    stock_code, description,
    ROUND(SUM(total_price), 2) AS revenue,
    SUM(quantity) AS units
FROM sales
GROUP BY stock_code, description
ORDER BY revenue DESC
LIMIT 10;

-- 3.11 Basket size distribution (distinct items per invoice)
DROP VIEW IF EXISTS basket_sizes;
CREATE VIEW basket_sizes AS
SELECT invoice_no, COUNT(DISTINCT description) AS n_items
FROM sales
GROUP BY invoice_no;

.print ''
.print '--- 3.11 Basket overview ---'
SELECT
    COUNT(*)                                            AS total_baskets,
    ROUND(AVG(n_items), 2)                              AS avg_items_per_basket,
    ROUND(100.0 * SUM(CASE WHEN n_items = 1 THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_single_item_baskets
FROM basket_sizes;

.print ''
.print '--- 3.12 Top co-purchased product pairs (min 30 co-occurrences) ---'
-- Self-join on shared invoice_no; a.description < b.description avoids double-counting/self-pairs.
SELECT
    a.description AS item_a,
    b.description AS item_b,
    COUNT(DISTINCT a.invoice_no) AS co_occurrences
FROM sales a
JOIN sales b
    ON a.invoice_no = b.invoice_no
   AND a.description < b.description
GROUP BY a.description, b.description
HAVING co_occurrences >= 30
ORDER BY co_occurrences DESC
LIMIT 15;
