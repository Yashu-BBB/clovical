import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Depends
from utils.db import supabase_admin, run_query
from utils.auth_utils import require_admin
from utils.cache import cache_get, cache_set

logger = logging.getLogger(__name__)
router = APIRouter()


def now_utc():
    return datetime.now(timezone.utc)

def start_of_day():
    return now_utc().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

def start_of_week():
    today = now_utc()
    return (today - timedelta(days=today.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

def start_of_month():
    return now_utc().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

def start_of_last_month():
    first = now_utc().replace(day=1)
    last_month_end = first - timedelta(days=1)
    return last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


@router.get("/overview")
async def analytics_overview(admin=Depends(require_admin)):
    cache_key = "analytics:overview"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        # All time - paginate to avoid memory issues at scale
        all_orders = []
        page = 0
        page_size = 1000
        while True:
            batch_res = await run_query(supabase_admin.table("orders").select("*").range(page * page_size, (page + 1) * page_size - 1))
            batch = batch_res.data or []
            all_orders.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
        orders = all_orders
        active = [o for o in orders if o["status"] not in ("cancelled","refunded")]
        total_revenue = sum(o["our_price"] for o in active)
        total_cost = sum(o["shopkeeper_price"] for o in active)
        total_profit = sum(o["profit"] for o in active)

        # Products
        total_products_res = await run_query(supabase_admin.table("products").select("id", count="exact"))
        total_products = total_products_res.count or 0

        # Reviews
        reviews_res = await run_query(supabase_admin.table("reviews").select("rating"))
        reviews = reviews_res.data or []
        avg_rating = sum(r["rating"] for r in reviews) / len(reviews) if reviews else 0

        # Unique buyers
        unique_buyers = len(set(o["customer_phone"] for o in orders))

        # Average order value
        aov = total_revenue / len(active) if active else 0

        # Time based
        today_orders = [o for o in active if o["created_at"] >= start_of_day()]
        week_orders = [o for o in active if o["created_at"] >= start_of_week()]
        month_orders = [o for o in active if o["created_at"] >= start_of_month()]
        last_month_orders = [o for o in active if start_of_last_month() <= o["created_at"] < start_of_month()]

        def summarize(ords):
            return {
                "count": len(ords),
                "revenue": sum(o["our_price"] for o in ords),
                "profit": sum(o["profit"] for o in ords)
            }

        # Status breakdown
        status_counts = {}
        for o in orders:
            s = o["status"]
            status_counts[s] = status_counts.get(s, 0) + 1

        # Payment breakdown
        upi_count = sum(1 for o in orders if o.get("payment_type") == "upi")
        cod_count = sum(1 for o in orders if o.get("payment_type") == "cod")

        # Refunds
        refund_pending = [o for o in orders if o.get("refund_status") == "pending"]

        # Category sales
        from collections import Counter
        category_res_raw = await run_query(supabase_admin.table("products").select("id,category"))
        category_res = category_res_raw.data or []
        prod_category = {p["id"]: p["category"] for p in category_res}
        category_sales = Counter()
        for o in active:
            cat = prod_category.get(str(o.get("product_id")), "Other")
            category_sales[cat] += 1

        # Top products
        prod_sales = Counter()
        for o in active:
            prod_sales[o["product_name"]] += 1
        top_products = prod_sales.most_common(5)

        # Top viewed products
        top_viewed_res = await run_query(supabase_admin.table("products").select("name,view_count").order("view_count", desc=True).limit(5))
        top_viewed = [(p["name"], p["view_count"]) for p in (top_viewed_res.data or [])]

        # Shopkeeper analytics
        shopkeepers_res = await run_query(supabase_admin.table("shopkeepers").select("*"))
        shopkeepers = shopkeepers_res.data or []
        sk_data = []
        total_owed = 0
        for sk in shopkeepers:
            code = f"#{sk['id']:03d}"
            sk_orders = [o for o in active if o.get("shopkeeper_code") == code]
            revenue = sum(o["our_price"] for o in sk_orders)
            cost = sum(o["shopkeeper_price"] for o in sk_orders)
            profit = sum(o["profit"] for o in sk_orders)
            amount_owed = cost  # what we owe shopkeeper
            total_owed += amount_owed
            sk_data.append({
                "code": code,
                "shop_name": sk["shop_name"],
                "shopkeeper_name": sk["shopkeeper_name"],
                "total_sold": len(sk_orders),
                "revenue": revenue,
                "profit": profit,
                "amount_owed": amount_owed
            })

        # Top cities
        city_count = Counter(o["customer_city"] for o in active)
        top_cities = city_count.most_common(5)

        # Peak hours
        hour_count = Counter()
        for o in orders:
            try:
                hour = int(o["created_at"][11:13])
                hour_count[hour] += 1
            except:
                pass

        # Daily revenue this month
        daily = {}
        for o in month_orders:
            day = o["created_at"][:10]
            if day not in daily:
                daily[day] = {"revenue": 0, "profit": 0}
            daily[day]["revenue"] += o["our_price"]
            daily[day]["profit"] += o["profit"]

        # Rating breakdown
        rating_breakdown = Counter(r["rating"] for r in reviews)

        cancellation_rate = (status_counts.get("cancelled", 0) / len(orders) * 100) if orders else 0

        result = {
            "all_time": {
                "revenue": total_revenue,
                "cost": total_cost,
                "profit": total_profit,
                "orders": len(active),
                "products": total_products,
                "unique_buyers": unique_buyers,
                "aov": aov,
                "reviews_count": len(reviews),
                "avg_rating": round(avg_rating, 1),
                "profit_margin": round(total_profit / total_revenue * 100, 1) if total_revenue else 0
            },
            "today": summarize(today_orders),
            "this_week": summarize(week_orders),
            "this_month": summarize(month_orders),
            "last_month": summarize(last_month_orders),
            "status_breakdown": status_counts,
            "payment_breakdown": {"upi": upi_count, "cod": cod_count},
            "category_sales": dict(category_sales),
            "top_products": top_products,
            "top_viewed": top_viewed,
            "shopkeepers": sk_data,
            "total_owed_to_shopkeepers": total_owed,
            "top_cities": top_cities,
            "peak_hours": dict(hour_count),
            "daily_revenue": daily,
            "rating_breakdown": dict(rating_breakdown),
            "cancellation_rate": round(cancellation_rate, 1),
            "refund_pending_count": len(refund_pending),
            "refund_pending_amount": sum(o["our_price"] for o in refund_pending)
        }
        await cache_set(cache_key, result, ttl=300)
        return result
    except Exception as e:
        logger.error(f"Analytics overview failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch analytics")

@router.get("/visitors")
async def visitor_stats(admin=Depends(require_admin)):
    """
    Visitor counts for the dedicated Visitors admin page — total, this
    month, this week, and today. Live (last-5-minutes) count is served
    separately by GET /api/admin/active-visitors, which is Redis-backed
    rather than a table count and already has its own endpoint.

    Uses count="exact" (a real SQL COUNT) rather than fetching rows and
    taking len(), since a plain .select() is capped at Supabase's default
    page size and would silently undercount once visitor volume grows.
    """
    cache_key = "analytics:visitors"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        total_res = await run_query(supabase_admin.table("visitors").select("id", count="exact"))
        today_res = await run_query(supabase_admin.table("visitors").select("id", count="exact").gte("created_at", start_of_day()))
        week_res = await run_query(supabase_admin.table("visitors").select("id", count="exact").gte("created_at", start_of_week()))
        month_res = await run_query(supabase_admin.table("visitors").select("id", count="exact").gte("created_at", start_of_month()))

        result = {
            "total": total_res.count or 0,
            "today": today_res.count or 0,
            "this_week": week_res.count or 0,
            "this_month": month_res.count or 0,
        }
        await cache_set(cache_key, result, ttl=120)
        return result
    except Exception as e:
        logger.error(f"Visitor stats failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch visitor stats")