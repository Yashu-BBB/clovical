import logging
from datetime import datetime, timedelta, timezone
from collections import Counter
from pydantic import BaseModel
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from utils.db import supabase_admin, run_query
from utils.auth_utils import require_shopkeeper
from utils.cache import (
    two_layer_get, two_layer_set, two_layer_clear_pattern,
    cache_clear_pattern, mem_clear_pattern,
)
from utils.notifications import check_out_of_stock

logger = logging.getLogger(__name__)
router = APIRouter()

# Fields shown to the shopkeeper for their own live products. Deliberately
# excludes our_price/mrp/profit — the shopkeeper only ever sees the price
# they get paid (shopkeeper_price), never the margin on top of it.
SHOPKEEPER_PRODUCT_FIELDS = "id,name,category,gender,shopkeeper_price,sizes,colors,size_stock,color_stock,stock,image,created_at"

# Same fields, plus what's needed to render per-size/per-colour stock
# controls on the shopkeeper's own Stock page.
SHOPKEEPER_STOCK_FIELDS = "id,name,category,image,sizes,colors,stock,size_stock,color_stock"


@router.get("/me")
async def me(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"shopkeeper:me:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("shopkeepers").select("id,shop_name,shopkeeper_name,contact")
            .eq("id", shopkeeper["shopkeeper_id"]).single()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Shopkeeper not found")
        data = {**res.data, "code": f"#{res.data['id']:03d}"}
        await two_layer_set(cache_key, data, redis_ttl=900, mem_ttl=120)
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch profile")


@router.get("/products")
async def my_products(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"shopkeeper:products:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("products").select(SHOPKEEPER_PRODUCT_FIELDS)
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("created_at", desc=True)
        )
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=300, mem_ttl=60)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper products: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch your products")


# ─── STOCK MANAGEMENT (shopkeeper's own live products only) ────────────────
# Mirrors the admin stock-list / stock-update endpoints in routers/products.py,
# but scoped to the requesting shopkeeper's own products only — a shopkeeper
# can never see or touch another shopkeeper's stock. Text/number only, no
# image handling, so this stays lightweight even on a phone connection.

@router.get("/stock-list")
async def my_stock_list(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"shopkeeper:stock-list:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("products").select(SHOPKEEPER_STOCK_FIELDS)
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("name")
        )
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=300, mem_ttl=30)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper stock list: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch your stock")


class ShopkeeperStockUpdate(BaseModel):
    stock: Optional[int] = None
    size_stock: Optional[dict] = None
    color_stock: Optional[dict] = None


async def _clear_shopkeeper_and_public_caches(shopkeeper_id: int):
    await two_layer_clear_pattern(f"shopkeeper:stock-list:{shopkeeper_id}")
    await two_layer_clear_pattern(f"shopkeeper:products:{shopkeeper_id}")
    # A stock edit changes the Stock Health section of /analytics — clear it
    # too so that page doesn't show a stale low/out-of-stock list for up to
    # 5 minutes. (Earnings/Sales sections of the same cached payload get
    # recomputed as a side effect, which is harmless.)
    await two_layer_clear_pattern(f"shopkeeper:analytics:{shopkeeper_id}")
    await cache_clear_pattern("products:*")
    await two_layer_clear_pattern("products:filter-options:")
    mem_clear_pattern("product:")


@router.put("/stock/{product_id}")
async def update_my_stock(product_id: str, data: ShopkeeperStockUpdate, shopkeeper=Depends(require_shopkeeper)):
    """Add/reduce stock (overall and/or per size/colour) on one of the
    shopkeeper's own live products. Ownership is verified before any write —
    a shopkeeper can never modify a product that isn't theirs."""
    try:
        owner_check = await run_query(
            supabase_admin.table("products").select("id,name,shopkeeper_id,stock,size_stock,color_stock").eq("id", product_id).single()
        )
        if not owner_check.data:
            raise HTTPException(status_code=404, detail="Product not found")
        if owner_check.data.get("shopkeeper_id") != shopkeeper["shopkeeper_id"]:
            raise HTTPException(status_code=403, detail="You can only manage stock for your own products")
        before_stock = owner_check.data

        updates = {}
        if data.stock is not None:
            updates["stock"] = max(0, data.stock)
        if data.size_stock is not None:
            updates["size_stock"] = {k: max(0, v) for k, v in data.size_stock.items()}
        if data.color_stock is not None:
            updates["color_stock"] = {k: max(0, v) for k, v in data.color_stock.items()}
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        res = await run_query(supabase_admin.table("products").update(updates).eq("id", product_id))
        await _clear_shopkeeper_and_public_caches(shopkeeper["shopkeeper_id"])
        logger.info(f"Shopkeeper {shopkeeper['shopkeeper_id']} updated stock for product {product_id}: {updates}")
        updated = res.data[0] if res.data else {}
        await check_out_of_stock(
            product_id, before_stock.get("name") or "Product",
            before_stock,
            {"stock": updated.get("stock"), "size_stock": updated.get("size_stock"), "color_stock": updated.get("color_stock")},
        )
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update stock for {product_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update stock")


# ─── ANALYTICS (shopkeeper's own earnings / sales / stock health) ──────────
# A NEW, read-only page — separate from the admin panel's own cross-shop
# /api/analytics/overview, and separate from what's already shown on this
# shopkeeper's own Products/Orders/Stock pages. Everything below is scoped
# to shopkeeper["shopkeeper_id"] and built only from shopkeeper_price — the
# fields selected from `orders` never include our_price/profit, and no
# other shopkeeper's or customer's data is ever touched.
#
# NOTE — "pending payout" (see PENDING_PAYOUT_NOTE below): the schema has
# no payout/settlement-tracking field anywhere (checked the `orders` table
# and every schema_*.sql migration — there is no settled_at / payout_status
# / paid_out column). Per the brief, this is approximated as delivered
# orders whose customer payment has already been collected (payment_status
# in received/verified). Because nothing in the schema is ever marked
# "settled to shopkeeper", this number is really "total collected on your
# behalf so far" — it will NOT shrink once Clovical actually pays a
# shopkeeper out, since there's nowhere to record that a payout happened.
# Real "amount still owed" tracking needs a new column/table before this
# figure can be trusted as anything more than an upper bound.
PENDING_PAYOUT_NOTE = (
    "Clovical doesn't yet track shopkeeper payouts as a separate step, so "
    "this shows everything collected from your delivered orders — it won't "
    "go down after a real payout until payout tracking is added."
)

# product/status/payment fields only — no our_price, no profit, no customer
# name/phone/address. Mirrors the field list already used by GET
# /api/orders/shopkeeper/mine.
SHOPKEEPER_ORDER_ANALYTICS_FIELDS = "id,product_id,product_name,size,color,shopkeeper_price,payment_status,status,created_at"

# "Sold"/"active" for sales-performance purposes mirrors the admin
# analytics overview's own definition of an active order.
_INACTIVE_ORDER_STATUSES = ("cancelled", "refunded")
_PAID_STATUSES = ("received", "verified")


def _day_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _start_of_week(dt: datetime) -> datetime:
    return (dt - timedelta(days=dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_month(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _start_of_prev_month(dt: datetime) -> datetime:
    last_month_end = _start_of_month(dt) - timedelta(days=1)
    return _start_of_month(last_month_end)


def _pct_change(current: int, prev: int) -> float:
    if prev:
        return round((current - prev) / prev * 100, 1)
    return 100.0 if current else 0.0


def _price(o: dict) -> float:
    return float(o.get("shopkeeper_price") or 0)


@router.get("/analytics")
async def my_analytics(shopkeeper=Depends(require_shopkeeper)):
    sk_id = shopkeeper["shopkeeper_id"]
    cache_key = f"shopkeeper:analytics:{sk_id}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached

        # All of this shopkeeper's orders. Paginated the same way as the
        # admin analytics overview (routers/analytics.py) — scoped to one
        # shopkeeper this is normally small, but stays correct at any
        # volume without pulling more columns than needed.
        all_orders = []
        page, page_size = 0, 1000
        while True:
            batch_res = await run_query(
                supabase_admin.table("orders").select(SHOPKEEPER_ORDER_ANALYTICS_FIELDS)
                .eq("shopkeeper_id", sk_id)
                .range(page * page_size, (page + 1) * page_size - 1)
            )
            batch = batch_res.data or []
            all_orders.extend(batch)
            if len(batch) < page_size:
                break
            page += 1

        now = datetime.now(timezone.utc)
        start_week = _start_of_week(now)
        start_prev_week = start_week - timedelta(days=7)
        start_month = _start_of_month(now)
        start_prev_month = _start_of_prev_month(now)
        start_trend = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)

        def created(o):
            return o.get("created_at") or ""

        active = [o for o in all_orders if o.get("status") not in _INACTIVE_ORDER_STATUSES]
        # Earnings only ever count DELIVERED orders, per the brief — a
        # shipped-but-not-delivered order isn't "earned" yet.
        delivered = [o for o in all_orders if o.get("status") == "delivered"]
        paid_delivered = [o for o in delivered if o.get("payment_status") in _PAID_STATUSES]

        # ── 1. EARNINGS ─────────────────────────────────────────────────
        total_earned = sum(_price(o) for o in delivered)
        month_earned = sum(_price(o) for o in delivered if created(o) >= start_month.isoformat())
        pending_payout = sum(_price(o) for o in paid_delivered)

        # Daily trend for the last 30 days. Bucketed by the order's
        # created_at (there's no separate "delivered_at" timestamp in the
        # schema to bucket by instead), for orders that are delivered.
        trend_totals = {}
        d = start_trend
        while d.date() <= now.date():
            trend_totals[_day_str(d)] = 0.0
            d += timedelta(days=1)
        for o in delivered:
            day = created(o)[:10]
            if day in trend_totals:
                trend_totals[day] += _price(o)
        trend_30d = [{"date": day, "earned": round(amt, 2)} for day, amt in sorted(trend_totals.items())]

        # ── 2. SALES PERFORMANCE ────────────────────────────────────────
        product_units = Counter()
        product_names = {}
        size_counts = Counter()
        color_counts = Counter()
        for o in active:
            key = o.get("product_id") or o.get("product_name")
            product_units[key] += 1
            product_names[key] = o.get("product_name") or "Unnamed product"
            if o.get("size"):
                size_counts[o["size"]] += 1
            if o.get("color"):
                color_counts[o["color"]] += 1

        units_by_product = [
            {"product_name": product_names[k], "units": v}
            for k, v in product_units.most_common()
        ]
        best_sizes = [{"size": s, "count": c} for s, c in size_counts.most_common()]
        best_colors = [{"color": col, "count": c} for col, c in color_counts.most_common()]

        week_orders = [o for o in active if created(o) >= start_week.isoformat()]
        prev_week_orders = [o for o in active if start_prev_week.isoformat() <= created(o) < start_week.isoformat()]
        month_orders = [o for o in active if created(o) >= start_month.isoformat()]
        prev_month_orders = [o for o in active if start_prev_month.isoformat() <= created(o) < start_month.isoformat()]

        # ── 3. STOCK HEALTH ─────────────────────────────────────────────
        # Same low-stock threshold as the admin panel's own dashboard
        # (routers/admin.py: 0 < stock <= 2 is "low", 0 is "out").
        prod_res = await run_query(
            supabase_admin.table("products").select("id,name,image,stock")
            .eq("shopkeeper_id", sk_id)
        )
        products = prod_res.data or []
        low_stock = [p for p in products if 0 < (p.get("stock") or 0) <= 2]
        out_of_stock = [p for p in products if (p.get("stock") or 0) == 0]

        pending_req_res = await run_query(
            supabase_admin.table("product_requests").select("id", count="exact")
            .eq("shopkeeper_id", sk_id).eq("status", "pending")
        )
        pending_requests_count = pending_req_res.count or 0

        result = {
            "earnings": {
                "total_earned": round(total_earned, 2),
                "this_month_earned": round(month_earned, 2),
                "pending_payout": round(pending_payout, 2),
                "pending_payout_note": PENDING_PAYOUT_NOTE,
                "trend_30d": trend_30d,
            },
            "sales": {
                "units_by_product": units_by_product,
                "best_sizes": best_sizes,
                "best_colors": best_colors,
                "orders_this_week": {
                    "count": len(week_orders), "prev_count": len(prev_week_orders),
                    "change_pct": _pct_change(len(week_orders), len(prev_week_orders)),
                },
                "orders_this_month": {
                    "count": len(month_orders), "prev_count": len(prev_month_orders),
                    "change_pct": _pct_change(len(month_orders), len(prev_month_orders)),
                },
            },
            "stock": {
                "low_stock": [{"id": p["id"], "name": p["name"], "image": p.get("image"), "stock": p.get("stock")} for p in low_stock],
                "out_of_stock": [{"id": p["id"], "name": p["name"], "image": p.get("image"), "stock": p.get("stock")} for p in out_of_stock],
                "pending_requests_count": pending_requests_count,
            },
        }
        await two_layer_set(cache_key, result, redis_ttl=300, mem_ttl=60)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper analytics for {sk_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch analytics")