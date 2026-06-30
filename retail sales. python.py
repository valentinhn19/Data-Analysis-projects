"""
Retail Sales Analysis
======================
Customer segmentation (RFM), sales trends, and market-basket analysis
on the UK Online Retail transactional dataset.

Pipeline: load raw data -> clean -> compute RFM segments -> compute
monthly/seasonal sales trends -> compute basket/association-rule style
co-purchase analysis -> print console summary -> save outputs that
feed the SQL script, dashboard, and Word summary.
"""

import json
from itertools import combinations
from collections import Counter

import pandas as pd
import numpy as np

RAW_PATH = "/mnt/user-data/uploads/retail_sales.csv"
CLEAN_CSV_PATH = "/mnt/user-data/outputs/retail_sales_clean.csv"
SUMMARY_JSON_PATH = "/home/claude/project/summary.json"

NON_PRODUCT_CODES = {
    "POST", "D", "DOT", "M", "BANK CHARGES", "S",
    "AMAZONFEE", "DCGSSBOY", "DCGSSGIRL", "PADS", "B", "CRUK"
}

# ---------------------------------------------------------------------------
# 1. LOAD
# ---------------------------------------------------------------------------
print("=" * 70)
print("STEP 1: LOAD RAW DATA")
print("=" * 70)
raw = pd.read_csv(RAW_PATH)
print(f"Raw rows: {len(raw):,}")

# ---------------------------------------------------------------------------
# 2. CLEAN
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("STEP 2: CLEAN")
print("=" * 70)

df = raw.copy()

# snake_case columns
df.columns = [
    "row_index", "invoice_no", "stock_code", "description", "quantity",
    "invoice_date", "unit_price", "customer_id", "country"
]

before = len(df)
# Drop rows with no CustomerID (can't attribute to a customer -> can't segment/basket)
df = df.dropna(subset=["customer_id"])
print(f"Dropped {before - len(df):,} rows with missing customer_id")

before = len(df)
# Remove cancellations (invoice_no starting with 'C')
df = df[~df["invoice_no"].astype(str).str.upper().str.startswith("C")]
print(f"Dropped {before - len(df):,} cancellation rows (invoice starts with 'C')")

before = len(df)
# Remove non-product stock codes (postage, fees, discounts, manual adjustments, etc.)
df = df[~df["stock_code"].astype(str).str.upper().isin(NON_PRODUCT_CODES)]
print(f"Dropped {before - len(df):,} non-product line items (postage/fees/adjustments)")

before = len(df)
# Remove non-positive quantity or price (returns/errors not already caught)
df = df[(df["quantity"] > 0) & (df["unit_price"] > 0)]
print(f"Dropped {before - len(df):,} rows with non-positive quantity/price")

before = len(df)
df = df.drop_duplicates()
print(f"Dropped {before - len(df):,} exact duplicate rows")

# Types
df["customer_id"] = df["customer_id"].astype(int)
df["invoice_date"] = pd.to_datetime(df["invoice_date"], format="%m/%d/%Y %H:%M")
df["description"] = df["description"].fillna("UNKNOWN").str.strip()
df["country"] = df["country"].str.strip()

# Derived column (cheap, keep in CSV)
df["total_price"] = (df["quantity"] * df["unit_price"]).round(2)

df = df.drop(columns=["row_index"]).reset_index(drop=True)

print(f"\nClean rows: {len(df):,}")
print(f"Unique customers: {df['customer_id'].nunique():,}")
print(f"Unique invoices: {df['invoice_no'].nunique():,}")
print(f"Date range: {df['invoice_date'].min().date()} to {df['invoice_date'].max().date()}")

df.to_csv(CLEAN_CSV_PATH, index=False)
print(f"\nSaved clean dataset -> {CLEAN_CSV_PATH}")

# ---------------------------------------------------------------------------
# 3. CUSTOMER SEGMENTATION (RFM)
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("STEP 3: CUSTOMER SEGMENTATION (RFM)")
print("=" * 70)

snapshot_date = df["invoice_date"].max() + pd.Timedelta(days=1)

rfm = df.groupby("customer_id").agg(
    last_purchase=("invoice_date", "max"),
    frequency=("invoice_no", "nunique"),
    monetary=("total_price", "sum"),
).reset_index()
rfm["recency"] = (snapshot_date - rfm["last_purchase"]).dt.days
rfm = rfm[["customer_id", "recency", "frequency", "monetary"]]

# Score 1-4 (4 = best) via quartiles. Rank ties before qcut (instead of qcut
# directly on raw values) so bin sizes stay even even when many customers
# share a value -- this mirrors how SQL's NTILE() window function buckets
# ties, keeping the Python and SQL segment counts consistent.
rfm["r_score"] = pd.qcut(rfm["recency"].rank(method="first"), 4, labels=[4, 3, 2, 1]).astype(int)
rfm["f_score"] = pd.qcut(rfm["frequency"].rank(method="first"), 4, labels=[1, 2, 3, 4]).astype(int)
rfm["m_score"] = pd.qcut(rfm["monetary"].rank(method="first"), 4, labels=[1, 2, 3, 4]).astype(int)
rfm["rfm_score"] = rfm["r_score"] + rfm["f_score"] + rfm["m_score"]


def segment_customer(row):
    r, f, m = row["r_score"], row["f_score"], row["m_score"]
    if r >= 4 and f >= 4 and m >= 4:
        return "Champions"
    if r >= 3 and f >= 3:
        return "Loyal Customers"
    if r >= 4 and f <= 2:
        return "New Customers"
    if r <= 2 and f >= 3 and m >= 3:
        return "At Risk"
    if r <= 2 and f <= 2 and m <= 2:
        return "Hibernating"
    return "Needs Attention"


rfm["segment"] = rfm.apply(segment_customer, axis=1)

seg_summary = rfm.groupby("segment").agg(
    customers=("customer_id", "count"),
    avg_recency_days=("recency", "mean"),
    avg_frequency=("frequency", "mean"),
    avg_monetary=("monetary", "mean"),
    total_revenue=("monetary", "sum"),
).round(2).sort_values("total_revenue", ascending=False)

seg_summary["pct_customers"] = (seg_summary["customers"] / seg_summary["customers"].sum() * 100).round(1)
seg_summary["pct_revenue"] = (seg_summary["total_revenue"] / seg_summary["total_revenue"].sum() * 100).round(1)

print(seg_summary.to_string())

top10_customers = rfm.sort_values("monetary", ascending=False).head(10)[
    ["customer_id", "recency", "frequency", "monetary", "segment"]
]
print("\nTop 10 customers by total spend:")
print(top10_customers.to_string(index=False))

# ---------------------------------------------------------------------------
# 4. SALES TRENDS
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("STEP 4: SALES TRENDS")
print("=" * 70)

df["year_month"] = df["invoice_date"].dt.to_period("M").astype(str)
monthly = df.groupby("year_month").agg(
    revenue=("total_price", "sum"),
    orders=("invoice_no", "nunique"),
    customers=("customer_id", "nunique"),
).round(2).reset_index()
monthly["aov"] = (monthly["revenue"] / monthly["orders"]).round(2)
print(monthly.to_string(index=False))

df["dow"] = df["invoice_date"].dt.day_name()
dow_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
by_dow = df.groupby("dow").agg(revenue=("total_price", "sum"), orders=("invoice_no", "nunique"))
by_dow = by_dow.reindex(dow_order).round(2)
print("\nRevenue by day of week:")
print(by_dow.to_string())

df["hour"] = df["invoice_date"].dt.hour
by_hour = df.groupby("hour").agg(revenue=("total_price", "sum"), orders=("invoice_no", "nunique")).round(2)
print("\nRevenue by hour of day:")
print(by_hour.to_string())

top_countries = df.groupby("country").agg(
    revenue=("total_price", "sum"), customers=("customer_id", "nunique")
).round(2).sort_values("revenue", ascending=False).head(10)
print("\nTop 10 countries by revenue:")
print(top_countries.to_string())

top_products = df.groupby(["stock_code", "description"]).agg(
    revenue=("total_price", "sum"), units=("quantity", "sum")
).round(2).sort_values("revenue", ascending=False).head(10).reset_index()
print("\nTop 10 products by revenue:")
print(top_products.to_string(index=False))

# ---------------------------------------------------------------------------
# 5. MARKET BASKET ANALYSIS
# ---------------------------------------------------------------------------
print("\n" + "=" * 70)
print("STEP 5: MARKET BASKET ANALYSIS")
print("=" * 70)

# Keep baskets with reasonable size to bound combinations (cap at 25 distinct items)
basket_items = df.groupby("invoice_no")["description"].apply(
    lambda x: sorted(set(x))
).reset_index(name="items")
basket_items["n_items"] = basket_items["items"].apply(len)

print(f"Total baskets (invoices): {len(basket_items):,}")
print(f"Average distinct items per basket: {basket_items['n_items'].mean():.2f}")
print(f"Single-item baskets: {(basket_items['n_items'] == 1).sum():,} "
      f"({(basket_items['n_items'] == 1).mean() * 100:.1f}%)")

multi = basket_items[basket_items["n_items"] >= 2]

item_counts = Counter()
for items in basket_items["items"]:
    item_counts.update(items)

n_baskets = len(basket_items)
pair_counts = Counter()
for items in multi["items"]:
    for pair in combinations(items, 2):
        pair_counts[pair] += 1

rules = []
for (a, b), co_count in pair_counts.items():
    support = co_count / n_baskets
    conf_a_b = co_count / item_counts[a]
    conf_b_a = co_count / item_counts[b]
    lift = support / ((item_counts[a] / n_baskets) * (item_counts[b] / n_baskets))
    rules.append({
        "item_a": a, "item_b": b, "co_occurrences": co_count,
        "support": round(support, 4),
        "confidence_a_to_b": round(conf_a_b, 4),
        "confidence_b_to_a": round(conf_b_a, 4),
        "lift": round(lift, 2),
    })

rules_df = pd.DataFrame(rules)
# Filter to meaningful pairs: co-occur at least 30 times
rules_df = rules_df[rules_df["co_occurrences"] >= 30]
top_lift = rules_df.sort_values("lift", ascending=False).head(15)
top_co = rules_df.sort_values("co_occurrences", ascending=False).head(15)

print("\nTop 15 product pairs by co-occurrence count:")
print(top_co[["item_a", "item_b", "co_occurrences", "support", "lift"]].to_string(index=False))

print("\nTop 15 product pairs by lift (min 30 co-occurrences):")
print(top_lift[["item_a", "item_b", "co_occurrences", "lift", "confidence_a_to_b"]].to_string(index=False))

basket_size_dist = basket_items["n_items"].value_counts().sort_index()
basket_size_dist = basket_size_dist[basket_size_dist.index <= 20]

# ---------------------------------------------------------------------------
# 6. SAVE SUMMARY FOR DASHBOARD / DOCX
# ---------------------------------------------------------------------------
summary = {
    "overview": {
        "total_rows": int(len(df)),
        "total_revenue": round(float(df["total_price"].sum()), 2),
        "unique_customers": int(df["customer_id"].nunique()),
        "unique_invoices": int(df["invoice_no"].nunique()),
        "unique_products": int(df["stock_code"].nunique()),
        "date_start": str(df["invoice_date"].min().date()),
        "date_end": str(df["invoice_date"].max().date()),
        "avg_order_value": round(float(df["total_price"].sum() / df["invoice_no"].nunique()), 2),
    },
    "segments": seg_summary.reset_index().to_dict(orient="records"),
    "top_customers": top10_customers.to_dict(orient="records"),
    "monthly_trend": monthly.to_dict(orient="records"),
    "by_dow": [
        {"dow": k, "revenue": (None if pd.isna(v) else v), "orders": (None if pd.isna(o) else int(o))}
        for k, v, o in zip(by_dow.reset_index()["dow"], by_dow.reset_index()["revenue"], by_dow.reset_index()["orders"])
    ],
    "by_hour": by_hour.reset_index().to_dict(orient="records"),
    "top_countries": top_countries.reset_index().to_dict(orient="records"),
    "top_products": top_products.to_dict(orient="records"),
    "basket_overview": {
        "total_baskets": int(n_baskets),
        "avg_items_per_basket": round(float(basket_items["n_items"].mean()), 2),
        "single_item_pct": round(float((basket_items["n_items"] == 1).mean() * 100), 1),
    },
    "basket_size_dist": [{"size": int(k), "count": int(v)} for k, v in basket_size_dist.items()],
    "top_pairs_by_cooccurrence": top_co[["item_a", "item_b", "co_occurrences", "support", "lift"]].to_dict(orient="records"),
    "top_pairs_by_lift": top_lift[["item_a", "item_b", "co_occurrences", "lift", "confidence_a_to_b"]].to_dict(orient="records"),
}

with open(SUMMARY_JSON_PATH, "w") as f:
    json.dump(summary, f, indent=2, default=str)

print(f"\nSaved analysis summary -> {SUMMARY_JSON_PATH}")
print("\n" + "=" * 70)
print("DONE")
print("=" * 70)
