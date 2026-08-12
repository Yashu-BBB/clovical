import logging
import requests
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address
from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import get_admin_from_request, require_admin, hash_password
from utils.nimbuspost import track_shipment, cancel_shipment, get_couriers, is_configured as nimbuspost_is_configured
from utils.cache import (
    cache_get, cache_set, cache_delete, two_layer_get, two_layer_set,
    two_layer_clear_pattern, mem_delete,
)
from utils import cache as cache_utils

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")
limiter = Limiter(key_func=get_remote_address)


def admin_or_redirect(request: Request):
    admin = get_admin_from_request(request)
    if not admin:
        return None
    return admin


@router.get("/dashboard-data")
@limiter.limit("30/minute")
async def dashboard_data(request: Request, admin=Depends(require_admin)):
    cache_key = "admin:dashboard"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        products_count_res = await run_query(supabase_admin.table("products").select("id", count="exact"))
        products_count = products_count_res.count or 0

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        today_orders_res = await run_query(
            supabase_admin.table("orders").select("*")
            .gte("created_at", today)
            .not_.eq("status", "cancelled")
        )
        today_orders = today_orders_res.data or []

        recent_res = await run_query(supabase_admin.table("orders").select("*").order("created_at", desc=True).limit(10))
        recent = recent_res.data or []

        low_stock_res = await run_query(supabase_admin.table("products").select("id,name,stock").lte("stock", 2).gt("stock", 0))
        low_stock = low_stock_res.data or []

        out_of_stock_res = await run_query(supabase_admin.table("products").select("id,name").eq("stock", 0))
        out_of_stock = out_of_stock_res.data or []

        refund_pending_res = await run_query(supabase_admin.table("orders").select("*").eq("refund_status", "pending"))
        refund_pending = refund_pending_res.data or []

        # Orders whose shopkeeper package PDF failed to even generate —
        # surfaced here so an admin can manually retry via
        # /admin/orders/{id}/resend-package-pdf instead of the shopkeeper
        # silently never having a packing slip waiting for them.
        failed_pdf_res = await run_query(
            supabase_admin.table("orders").select("id,customer_name,product_name,created_at").eq("package_pdf_status", "failed")
        )
        failed_package_pdfs = failed_pdf_res.data or []

        # Orders whose package PDF is built and waiting — these are NOT sent
        # anywhere automatically; they're downloaded on demand from the
        # admin/shopkeeper panel (see .../package-pdf in routers/orders.py).
        ready_pdf_res = await run_query(
            supabase_admin.table("orders").select("id", count="exact").eq("package_pdf_status", "ready")
        )
        packages_awaiting_pickup = ready_pdf_res.count or 0

        # NimbusPost shipment stats
        shipments_today_res = await run_query(
            supabase_admin.table("orders").select("id", count="exact")
            .gte("created_at", today).eq("shipping_status", "created")
        )
        shipments_today = shipments_today_res.count or 0

        pending_shipments_res = await run_query(
            supabase_admin.table("orders").select("id", count="exact")
            .eq("status", "confirmed").is_("nimbuspost_awb", "null")
        )
        pending_shipments = pending_shipments_res.count or 0

        in_transit_shipments_res = await run_query(
            supabase_admin.table("orders").select("id", count="exact")
            .eq("status", "shipped").not_.is_("nimbuspost_awb", "null")
        )
        in_transit_shipments = in_transit_shipments_res.count or 0

        result = {
            "total_products": products_count,
            "orders_today": len(today_orders),
            "revenue_today": sum(o["our_price"] for o in today_orders),
            "profit_today": sum(o["profit"] for o in today_orders),
            "recent_orders": recent,
            "low_stock": low_stock,
            "out_of_stock": out_of_stock,
            "refund_pending": refund_pending,
            "shipments_today": shipments_today,
            "pending_shipments": pending_shipments,
            "in_transit_shipments": in_transit_shipments,
            "failed_package_pdfs": failed_package_pdfs,
            "packages_awaiting_pickup": packages_awaiting_pickup,
        }
        await cache_set(cache_key, result, ttl=120)
        return result
    except Exception as e:
        logger.error(f"Dashboard data failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard data")


@router.get("/cache-stats")
@limiter.limit("10/minute")
async def cache_stats(request: Request, admin=Depends(require_admin)):
    """Shows current cache status for admin monitoring."""
    import time
    active_mem_keys = [
        k for k, (_, exp) in cache_utils._mem_cache.items()
        if time.time() < exp
    ]
    return {
        "memory_cache": {
            "total_keys": len(active_mem_keys),
            "keys": active_mem_keys
        },
        "redis": "connected" if cache_utils.redis_client else "disconnected"
    }


@router.post("/change-password")
async def change_password(
    request: Request,
    admin=Depends(require_admin)
):
    data = await request.json()
    new_pass = data.get("password", "")
    if len(new_pass) < 8:
        raise HTTPException(status_code=400, detail="Password too short")
    hashed = hash_password(new_pass)
    await run_query(supabase_admin.table("admins").update({"password": hashed}).eq("username", admin["sub"]))
    return {"success": True}


# ─── Order PDF Export ───────────────────────────────────────────────────────

@router.get("/orders/{order_id}/pdf-data")
async def order_pdf_data(order_id: str, admin=Depends(require_admin)):
    """
    Returns all data needed for the admin Orders page's "Download PDF"
    button. The PDF itself is generated client-side with jsPDF — this
    endpoint just assembles the order, product, customer, payment and
    shipping fields it needs, grouped to match the PDF's sections.
    """
    try:
        order_res = await run_query(supabase_admin.table("orders").select("*").eq("id", order_id).single())
        order = order_res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Category isn't denormalized onto the order row, so fetch it from
        # the product. Best-effort — a missing/deleted product shouldn't
        # block the PDF from generating.
        category = None
        if order.get("product_id"):
            try:
                prod_res = await run_query(
                    supabase_admin.table("products").select("category").eq("id", order["product_id"]).maybe_single()
                )
                category = (prod_res.data or {}).get("category")
            except Exception as e:
                logger.warning(f"Could not fetch product category for order {order_id}: {e}")

        delivery_fee = order.get("delivery_fee") or 0
        amount_paid = (order.get("our_price") or 0) + delivery_fee

        return {
            "order": {
                "id": order["id"],
                "created_at": order.get("created_at"),
                "status": order.get("status"),
            },
            "product": {
                "image": order.get("product_image"),
                "name": order.get("product_name"),
                "category": category,
                "size": order.get("size"),
                "color": order.get("color"),
                "shopkeeper_code": order.get("shopkeeper_code"),
                "our_price": order.get("our_price"),
                "profit": order.get("profit"),
            },
            "customer": {
                "name": order.get("customer_name"),
                "phone": order.get("customer_phone"),
                "address": order.get("customer_address"),
                "city": order.get("customer_city"),
                "pincode": order.get("customer_pincode"),
            },
            "payment": {
                "type": order.get("payment_type"),
                "status": order.get("payment_status"),
                "delivery_fee": delivery_fee,
                "amount_paid": amount_paid,
            },
            "shipping": {
                "courier_name": order.get("courier_name"),
                "tracking_id": order.get("tracking_id"),
                "awb": order.get("nimbuspost_awb"),
                "shipment_id": order.get("nimbuspost_shipment_id"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PDF data fetch failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch order data")


# ─── NimbusPost Shipment Endpoints ─────────────────────────────────────────

@router.get("/orders/{order_id}/label")
async def get_shipment_label(order_id: str, admin=Depends(require_admin)):
    cache_key = f"order:{order_id}:label"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        res = await run_query(supabase_admin.table("orders").select("label_url").eq("id", order_id).single())
        order = res.data
        if not order or not order.get("label_url"):
            raise HTTPException(status_code=404, detail="No shipping label available for this order")
        result = {"label_url": order["label_url"]}
        await cache_set(cache_key, result, ttl=3600)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Label fetch failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch label")


@router.post("/orders/{order_id}/ship")
async def ship_order(order_id: str, admin=Depends(require_admin)):
    # Import here (not at module top) to avoid a circular import between
    # routers/admin.py and routers/orders.py
    from routers.orders import create_shipment_for_order

    try:
        res = await run_query(supabase_admin.table("orders").select("*").eq("id", order_id).single())
        order = res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        if order.get("nimbuspost_awb"):
            raise HTTPException(status_code=400, detail="Shipment already created for this order")

        if not nimbuspost_is_configured():
            raise HTTPException(status_code=400, detail="NimbusPost not configured")

        result = await create_shipment_for_order(order)
        if not result:
            raise HTTPException(
                status_code=502,
                detail="Shipment creation failed, or a shipment attempt for this order was already in progress — check the order's shipping status and retry if it's still not 'created'"
            )

        logger.info(f"Admin {admin['sub']} manually created shipment for order {order_id}: AWB {result['awb']}")
        return {"success": True, **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual shipment creation failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create shipment")


@router.post("/orders/{order_id}/resend-package-pdf")
async def resend_package_pdf(order_id: str, admin=Depends(require_admin)):
    """
    Manually re-triggers shopkeeper package PDF GENERATION for one order —
    for when it's stuck at package_pdf_status='failed' or an admin just
    wants to rebuild it. Re-fetches NimbusPost's label fresh from label_url
    if the order already has one, so the barcode/AWB on page 2 is still
    correct.

    This does NOT send anything anywhere — it only (re)builds the PDF and
    leaves it at package_pdf_status='ready'. It's picked up from there by
    downloading it on demand from the admin/shopkeeper panel (see
    .../package-pdf in routers/orders.py); there is no automatic push.
    """
    from routers.orders import _generate_shopkeeper_package_pdf

    try:
        res = await run_query(supabase_admin.table("orders").select("*").eq("id", order_id).single())
        order = res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        shopkeeper_id = order.get("shopkeeper_id")
        if not shopkeeper_id:
            raise HTTPException(status_code=400, detail="Order has no shopkeeper assigned")

        sk_res = await run_query(supabase_admin.table("shopkeepers").select("*").eq("id", shopkeeper_id).single())
        shopkeeper = sk_res.data
        if not shopkeeper:
            raise HTTPException(status_code=404, detail="Shopkeeper not found")

        label_bytes = None
        if order.get("label_url"):
            try:
                label_resp = await run_blocking(requests.get, order["label_url"], timeout=15)
                label_resp.raise_for_status()
                label_bytes = label_resp.content
            except Exception as e:
                logger.warning(f"Could not refetch NimbusPost label for resend on order {order_id}: {e}")

        await _generate_shopkeeper_package_pdf(order, shopkeeper, label_bytes)

        # Re-read so the caller sees the outcome (ready/failed) immediately.
        updated = await run_query(supabase_admin.table("orders").select("package_pdf_status").eq("id", order_id).single())
        status = (updated.data or {}).get("package_pdf_status")
        if status != "ready":
            raise HTTPException(status_code=502, detail="PDF regeneration failed — check server logs and retry")

        logger.info(f"Admin {admin['sub']} regenerated package PDF for order {order_id} (queued for shopkeeper pickup)")
        return {"success": True, "status": "ready"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Package PDF resend failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to resend package PDF")


@router.post("/orders/{order_id}/cancel-shipment")
async def cancel_order_shipment(order_id: str, admin=Depends(require_admin)):
    try:
        res = await run_query(supabase_admin.table("orders").select("nimbuspost_awb").eq("id", order_id).single())
        order = res.data
        if not order or not order.get("nimbuspost_awb"):
            raise HTTPException(status_code=404, detail="No NimbusPost shipment found for this order")

        ok = await run_blocking(cancel_shipment, order["nimbuspost_awb"])
        if not ok:
            raise HTTPException(status_code=502, detail="NimbusPost cancellation failed — please retry")

        await run_query(supabase_admin.table("orders").update({"shipping_status": "cancelled"}).eq("id", order_id))
        logger.info(f"Admin {admin['sub']} cancelled shipment for order {order_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Shipment cancellation failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to cancel shipment")


@router.get("/orders/{order_id}/track")
async def track_order_shipment(order_id: str, admin=Depends(require_admin)):
    try:
        res = await run_query(supabase_admin.table("orders").select("nimbuspost_awb").eq("id", order_id).single())
        order = res.data
        if not order or not order.get("nimbuspost_awb"):
            raise HTTPException(status_code=404, detail="No NimbusPost shipment found for this order")

        tracking = await run_blocking(track_shipment, order["nimbuspost_awb"])
        if tracking is None:
            raise HTTPException(status_code=502, detail="NimbusPost tracking unavailable right now — please retry")

        return tracking
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tracking fetch failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch tracking info")


@router.get("/nimbuspost/test-connection")
async def test_nimbuspost_connection(admin=Depends(require_admin)):
    """
    Diagnostic endpoint: confirms the Bearer auth (email+password login)
    works end-to-end by hitting the courier-list endpoint. Separate from
    pickup address registration, which uses different (static-key) auth
    — use this to isolate whether an issue is auth-wide or specific to
    the warehouse/pickup-address call.
    """
    result = await run_blocking(get_couriers)
    logger.info(f"NimbusPost connection test by admin {admin['sub']}: {result}")
    return result


# ─── Settings (manual/auto shipment mode) ──────────────────────────────────

class SettingUpdate(BaseModel):
    value: str


@router.get("/settings/{key}")
async def get_setting(key: str, admin=Depends(require_admin)):
    cache_key = f"settings:{key}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(supabase_admin.table("settings").select("*").eq("key", key).maybe_single())
        result = res.data or {"key": key, "value": None}
        await two_layer_set(cache_key, result, redis_ttl=300, mem_ttl=120)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch setting {key}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch setting")


@router.put("/settings/{key}")
async def update_setting(key: str, data: SettingUpdate, admin=Depends(require_admin)):
    try:
        await run_query(supabase_admin.table("settings").upsert({"key": key, "value": data.value}))
        logger.info(f"Setting updated: {key} = {data.value} by admin {admin['sub']}")
        await two_layer_clear_pattern("settings:")
        mem_delete("settings:delivery_fee")
        mem_delete("settings:girls_section_enabled")
        mem_delete("public_settings")
        await cache_delete("public_settings")
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to update setting {key}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update setting")


# ─── Public Settings (no auth — for customer-facing pages) ─────────────────

@router.get("/public-settings")
async def public_settings():
    """Public endpoint — returns non-sensitive settings for frontend.
    No auth required. Cached 5 min in Redis / 2 min in memory.
    """
    cache_key = "public_settings"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached

        res = await run_query(
            supabase_admin.table("settings").select("key,value").in_(
                "key", ["girls_section_enabled", "delivery_fee"]
            )
        )
        result = {row["key"]: row["value"] for row in (res.data or [])}
        # Ensure defaults if rows don't exist yet
        result.setdefault("girls_section_enabled", "false")
        result.setdefault("delivery_fee", "0")
        await two_layer_set(cache_key, result, redis_ttl=300, mem_ttl=120)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch public settings: {e}", exc_info=True)
        return {"girls_section_enabled": "false", "delivery_fee": "0"}

@router.get("/active-visitors")
async def active_visitors(admin=Depends(require_admin)):
    """Returns count of visitors active in last 5 minutes."""
    from datetime import datetime, timezone
    try:
        from utils.cache import redis_client
        if redis_client:
            keys = await redis_client.keys("active_visitor:*")
            return {
                "count": len(keys),
                "window": "5 minutes",
                "updated_at": datetime.now(timezone.utc).isoformat()
            }
        return {"count": 0, "window": "5 minutes", "error": "Redis not available"}
    except Exception as e:
        logger.error(f"Active visitors check failed: {e}", exc_info=True)
        return {"count": 0, "window": "5 minutes", "error": str(e)}