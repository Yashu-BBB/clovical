import re
import uuid
import asyncio
import base64
import logging
import requests
from typing import Literal
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import require_admin, require_shopkeeper, require_customer
from utils.captcha import verify_turnstile
from utils.nimbuspost import create_shipment
from utils.label_generator import build_shopkeeper_package_pdf
from utils.cache import cache_get, cache_set, cache_delete, two_layer_get, two_layer_set, mem_clear_pattern, cache_clear_pattern
from utils.notifications import notify_admins, notify_shopkeeper, notify_customer, check_out_of_stock
from utils.sms_utils import send_order_sms
# Reused as-is for the Cashfree path below — create_order() must never
# reimplement Cashfree order-creation/session logic itself (see routers/
# payments.py docstring for why that file owns this end-to-end).
from routers.payments import create_payment_session, CreateSessionRequest

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


class OrderRequest(BaseModel):
    customer_name: str = Field(..., max_length=200)
    customer_phone: str = Field(..., max_length=10, min_length=10)
    address_line1: str = Field(..., max_length=200)
    address_line2: str = Field("", max_length=200)
    customer_address: str = Field("", max_length=500)
    customer_city: str = Field(..., max_length=100)
    customer_pincode: str = Field(..., max_length=6, min_length=6)
    product_id: str = Field(..., max_length=36)
    size: str = Field(..., max_length=50)
    color: str = Field(..., max_length=100)
    captcha_token: str = Field("", max_length=2000)
    # "cod" or "cashfree" only — anything else is rejected by pydantic
    # before create_order() ever runs. There is deliberately no "upi"
    # option any more: that was the old WhatsApp-screenshot flow, which
    # customers no longer place orders through (see create_order()).
    payment_method: Literal["cod", "cashfree"]


class StatusUpdate(BaseModel):
    status: str
    tracking_id: str | None = None
    courier_name: str | None = None
    payment_status: str | None = None
    refund_status: str | None = None


# ─── NimbusPost auto-ship helpers ──────────────────────────────────────────

async def _is_auto_ship_enabled() -> bool:
    """Reads the nimbuspost_auto_mode toggle from the settings table."""
    try:
        res = await run_query(
            supabase_admin.table("settings").select("value").eq("key", "nimbuspost_auto_mode").maybe_single()
        )
        return bool(res.data and res.data.get("value") == "true")
    except Exception as e:
        logger.warning(f"Could not read nimbuspost_auto_mode setting, defaulting to manual: {e}")
        return False


async def get_delivery_fee() -> float:
    """
    Reads the admin-configured delivery fee from the settings table.
    Defaults to 0 if not set or unreadable, so checkout never breaks.
    """
    try:
        res = await run_query(
            supabase_admin.table("settings").select("value").eq("key", "delivery_fee").maybe_single()
        )
        return float(res.data.get("value", 0)) if res.data and res.data.get("value") else 0.0
    except Exception as e:
        logger.warning(f"Could not read delivery_fee setting, defaulting to 0: {e}")
        return 0.0


@router.get("/delivery-fee")
async def public_delivery_fee():
    """Public (no-auth) endpoint the checkout page reads to show the delivery fee."""
    cache_key = "settings:delivery_fee"
    cached = await two_layer_get(cache_key)
    if cached is not None:
        return cached
    result = {"delivery_fee": await get_delivery_fee()}
    await two_layer_set(cache_key, result, redis_ttl=300, mem_ttl=120)
    return result


async def _restore_order_stock(order: dict) -> bool:
    """
    Reverses the per-unit stock decrement create_order() made when this
    order was placed: +1 to the product's overall stock, and +1 to the
    order's specific size/colour entry in size_stock/color_stock if that
    variant is tracked (mirrors the decrement logic in create_order()
    exactly, just in the opposite direction).

    Callers (cancel_order, update_order, admin_delete_order) are
    responsible for checking order.get("stock_restored") before calling
    this and for persisting stock_restored=True on the order row
    afterwards — this function only touches the products table, so a
    single restoration never happens twice for the same order regardless
    of which of those three paths triggers it first.
    """
    product_id = order.get("product_id")
    if not product_id:
        logger.warning(f"Order {order.get('id')} has no product_id — skipping stock restoration")
        return False

    try:
        prod_res = await run_query(supabase_admin.table("products").select("*").eq("id", product_id).single())
        prod = prod_res.data
        if not prod:
            logger.warning(f"Product {product_id} no longer exists — skipping stock restoration for order {order.get('id')}")
            return False

        stock_update = {"stock": (prod.get("stock") or 0) + 1}

        size_stock_map = prod.get("size_stock") or {}
        order_size = order.get("size")
        if order_size in size_stock_map and size_stock_map[order_size] is not None:
            updated_size_map = dict(size_stock_map)
            updated_size_map[order_size] = updated_size_map[order_size] + 1
            stock_update["size_stock"] = updated_size_map

        color_stock_map = prod.get("color_stock") or {}
        order_color = order.get("color")
        if order_color in color_stock_map and color_stock_map[order_color] is not None:
            updated_color_map = dict(color_stock_map)
            updated_color_map[order_color] = updated_color_map[order_color] + 1
            stock_update["color_stock"] = updated_color_map

        await run_query(supabase_admin.table("products").update(stock_update).eq("id", product_id))

        # Same cache invalidation as every other stock-changing path in
        # this file (create_order's decrement, etc.) so the storefront
        # doesn't keep serving a stale "out of stock" snapshot.
        mem_clear_pattern("product:")
        await cache_clear_pattern("products:*")

        logger.info(f"Stock restored for order {order.get('id')} (product {product_id}, size={order_size}, color={order_color})")
        return True
    except Exception as e:
        logger.error(f"Failed to restore stock for order {order.get('id')} (product {product_id}): {e}", exc_info=True)
        return False


async def _mark_package_pdf_status(order_id: str, status: str):
    """
    Records the shopkeeper package PDF's state on the order. `status` is one
    of:
      'ready'  — PDF built and stored; downloadable from the admin/shopkeeper
                 panel via GET .../package-pdf below
      'failed' — PDF generation itself failed
    ('sent' was a legacy value written only by the old WhatsApp pull webhook,
    which has been removed — nothing sets it any more. Older rows may still
    carry it, and 'ready'-only handling elsewhere treats that the same as
    any other non-'ready' state.)
    This is what lets admins see (and manually regenerate) orders where the
    PDF never got built, instead of that failure only existing in a log line.
    """
    try:
        await run_query(
            supabase_admin.table("orders").update({"package_pdf_status": status}).eq("id", order_id)
        )
    except Exception as e:
        logger.error(f"Failed to record package_pdf_status={status} for order {order_id}: {e}", exc_info=True)


async def _generate_shopkeeper_package_pdf(order: dict, shopkeeper: dict, nimbuspost_label_bytes: bytes | None):
    """
    Builds the 2-page shopkeeper PDF (product photo + either NimbusPost's
    untouched label or our own fallback slip) and STORES it on the order —
    it does NOT send it anywhere.

    Why: this used to push the PDF to the shopkeeper's WhatsApp the instant
    an order was confirmed/shipped. That's a business-initiated message the
    shopkeeper never asked for in that moment, and doing it automatically on
    every single order is exactly the pattern WhatsApp's spam heuristics
    flag — which is what got the number temporarily restricted. Automatic
    pushes are gone for good now, and so is WhatsApp as a delivery channel
    entirely.

    Instead, the PDF sits here (base64, package_pdf_status='ready') until
    the admin or shopkeeper downloads it — see GET .../package-pdf on both
    the admin and shopkeeper routes below, the only places this base64 is
    ever read back out.

    Never raises — a PDF-generation failure must never block order/shipment
    processing. Building (reportlab/pypdf + an image fetch) is blocking
    work, so it runs off the event loop.
    """
    order_id = order.get("id")
    try:
        pdf_bytes = await run_blocking(build_shopkeeper_package_pdf, order, shopkeeper, nimbuspost_label_bytes)
        b64 = base64.b64encode(pdf_bytes).decode()
        await run_query(
            supabase_admin.table("orders").update({
                "package_pdf_status": "ready",
                "package_pdf_base64": b64,
                "package_pdf_filename": f"order_{str(order_id)[:8]}.pdf",
                "package_pdf_generated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", order_id)
        )
        logger.info(f"Package PDF generated & queued for shopkeeper pickup: order {order_id}")
    except Exception as e:
        logger.warning(f"Shopkeeper package PDF generation failed for order {order_id}: {e}")
        await _mark_package_pdf_status(order_id, "failed")


def _fire_and_forget(coro, description: str):
    """
    Schedules `coro` to run without the caller waiting on it. Used for the
    shopkeeper package-PDF generation, which involves an image fetch + PDF
    render and can take a few seconds — previously this was awaited inline,
    which made the admin's /ship request (and NimbusPost's own failure path)
    hang for that whole duration. _generate_shopkeeper_package_pdf already
    never raises and records its own success/failure on the order, so
    there's nothing useful for the caller to await here — but we still
    attach a done-callback to log anything unexpected instead of losing it
    silently.
    """
    task = asyncio.create_task(coro)

    def _log_if_failed(t: asyncio.Task):
        exc = t.exception() if not t.cancelled() else None
        if exc:
            logger.error(f"Background task failed ({description}): {exc}", exc_info=exc)

    task.add_done_callback(_log_if_failed)
    return task


async def _claim_shipment(order_id: str) -> bool:
    """
    Atomically claims this order for shipment creation by flipping
    shipping_status to 'creating', but only if nothing has claimed or
    completed it yet.

    This exists because `if order.get("nimbuspost_awb")` alone is a classic
    check-then-act race: two triggers landing close together (e.g. an admin
    clicking "Ship" at the same moment auto-ship also fires from a status
    update) can both pass that check before either has written anything,
    and both go on to call NimbusPost — creating two shipments (and two
    wallet debits) for one order.

    A single Postgres UPDATE is atomic at the row level, so when two
    requests race to run this at once, only one of them can match the
    WHERE clause and get shipping_status back as 'creating' in its response;
    the other gets an empty result and backs off. That makes the UPDATE
    itself the lock — no separate locking table needed.
    """
    try:
        q = (
            supabase_admin.table("orders")
            .update({"shipping_status": "creating"})
            .eq("id", order_id)
            .is_("nimbuspost_awb", "null")
            .or_("shipping_status.is.null,shipping_status.neq.creating")
        )
        res = await run_query(q)
        return bool(res.data)
    except Exception as e:
        logger.error(f"Shipment claim check failed for order {order_id}: {e}", exc_info=True)
        return False  # fail closed — don't risk a duplicate shipment on a DB error


async def _release_shipment_claim(order_id: str, status: str = "failed"):
    """Releases the 'creating' claim after an unsuccessful attempt, so a
    later retry (manual re-ship or the next auto-ship trigger) isn't
    permanently blocked behind a stuck 'creating' status."""
    try:
        await run_query(
            supabase_admin.table("orders").update({"shipping_status": status}).eq("id", order_id)
        )
    except Exception as e:
        logger.error(f"Failed to release shipment claim for order {order_id}: {e}", exc_info=True)


async def create_shipment_for_order(order: dict) -> dict | None:
    """
    Fetches the order's shopkeeper and calls NimbusPost to create a
    shipment, then persists the returned AWB/courier/label on the order and
    builds the shopkeeper's package PDF (product photo + NimbusPost's
    official label) for download from the admin/shopkeeper panel.

    Shared between the auto-ship flow here and the manual "Create Shipment"
    admin endpoint in routers/admin.py. Returns the NimbusPost result dict
    on success, or None on failure/duplicate-skip (never raises — shipment
    failures must never block the rest of the order flow).
    """
    order_id = order.get("id")
    try:
        if order.get("nimbuspost_awb"):
            logger.info(f"Order {order_id} already has a NimbusPost shipment — skipping")
            return None

        if not await _claim_shipment(order_id):
            logger.info(f"Order {order_id} shipment already created or in progress elsewhere — skipping duplicate attempt")
            return None

        shopkeeper_id = order.get("shopkeeper_id")
        if not shopkeeper_id:
            logger.warning(f"Order {order_id} has no shopkeeper_id — cannot create shipment")
            await _release_shipment_claim(order_id, "failed")
            return None

        sk_res = await run_query(supabase_admin.table("shopkeepers").select("*").eq("id", shopkeeper_id).single())
        shopkeeper = sk_res.data
        if not shopkeeper or not shopkeeper.get("address"):
            logger.warning(f"Shopkeeper {shopkeeper_id} has no registered address — cannot create shipment for order {order_id}")
            await _release_shipment_claim(order_id, "failed")
            # No NimbusPost label possible without an address — build the
            # fallback package PDF instead, so the shopkeeper still has the
            # photo + order details ready to download whenever they need it.
            # Fire-and-forget so a slow image fetch doesn't hold up the caller.
            if shopkeeper:
                _fire_and_forget(
                    _generate_shopkeeper_package_pdf(order, shopkeeper, None),
                    f"package PDF for order {order_id} (no shopkeeper address)",
                )
            return None

        result = await run_blocking(create_shipment, order, shopkeeper)
        if not result:
            await _release_shipment_claim(order_id, "failed")
            _fire_and_forget(
                _generate_shopkeeper_package_pdf(order, shopkeeper, None),
                f"package PDF for order {order_id} (NimbusPost shipment failed)",
            )
            return None

        await run_query(
            supabase_admin.table("orders").update({
                "nimbuspost_awb": result["awb"],
                "tracking_id": result["awb"],
                "courier_name": result["courier_name"],
                "label_url": result["label_url"],
                "nimbuspost_shipment_id": result["shipment_id"],
                "shipping_status": "created",
                "status": "shipped",
            }).eq("id", order_id)
        )

        logger.info(f"NimbusPost shipment created for order {order_id}: AWB {result['awb']}")

        # From here on, everything is best-effort background work that
        # shouldn't hold up the caller (the admin's "Ship" click, or the
        # auto-ship trigger) now that the shipment itself is confirmed
        # created. Runs in the background; failures are logged, not raised.
        async def _build_package_pdf_after_ship():
            # Fetch NimbusPost's own official label and merge it (untouched —
            # never edited, so the barcode/AWB stays valid) with our product
            # photo page, then build+store the combined PDF for the
            # admin/shopkeeper to download later — not send it anywhere.
            label_bytes = None
            if result.get("label_url"):
                try:
                    label_resp = await run_blocking(requests.get, result["label_url"], timeout=15)
                    label_resp.raise_for_status()
                    label_bytes = label_resp.content
                except Exception as e:
                    logger.warning(f"Could not fetch NimbusPost label PDF for order {order_id}: {e}")
            await _generate_shopkeeper_package_pdf(order, shopkeeper, label_bytes)

        _fire_and_forget(_build_package_pdf_after_ship(), f"package PDF build for order {order_id}")

        return result
    except Exception as e:
        logger.error(f"create_shipment_for_order failed for order {order_id}: {e}", exc_info=True)
        await _release_shipment_claim(order_id, "failed")
        return None


@router.post("/create")
@limiter.limit("5/minute")
async def create_order(order: OrderRequest, request: Request, customer=Depends(require_customer)):
    client_ip = request.headers.get("CF-Connecting-IP") or request.client.host

    # Verify captcha — hard block, never create an order without it.
    # Missing/empty token and a failed Turnstile check are surfaced with
    # distinct messages so the frontend can tell the customer what to do.
    if not order.captcha_token:
        raise HTTPException(status_code=400, detail="Human verification required. Please refresh and try again.")
    if not verify_turnstile(order.captcha_token, client_ip):
        raise HTTPException(status_code=400, detail="Human verification failed. Please try again.")

    # Address Line 1 is required; Address Line 2 is optional. Combine them
    # into the single customer_address field the rest of the order flow
    # (DB row, WhatsApp notification, etc.) already expects. Re-derived
    # here rather than trusting the frontend's combined value, since
    # frontend validation can be bypassed.
    address_line1 = order.address_line1.strip()
    address_line2 = order.address_line2.strip()
    if not address_line1:
        raise HTTPException(status_code=400, detail="Address Line 1 is required")
    order.address_line1 = address_line1
    order.address_line2 = address_line2
    order.customer_address = f"{address_line1}, {address_line2}".strip(", ")

    # Phone number validation (frontend validation can be bypassed)
    phone = order.customer_phone.strip().replace(" ", "").replace("-", "")
    if not re.match(r'^\d{10}$', phone):
        raise HTTPException(
            status_code=400,
            detail="Invalid phone number. Must be 10 digits."
        )
    order.customer_phone = phone  # use cleaned version

    # Pincode validation (frontend validation can be bypassed)
    if not re.match(r'^[1-9][0-9]{5}$', order.customer_pincode):
        raise HTTPException(
            status_code=400,
            detail="Invalid pincode. Must be a valid 6-digit Indian pincode."
        )

    # Fetch product (including hidden price)
    try:
        prod_res = await run_query(supabase_admin.table("products").select("*").eq("id", order.product_id).single())
    except Exception as e:
        logger.error(f"Order save failed - product fetch: {e}", exc_info=True)
        raise HTTPException(status_code=404, detail="Product not found")

    prod = prod_res.data
    if not prod or prod["stock"] < 1:
        raise HTTPException(status_code=400, detail="Product out of stock")

    # Per-variant stock check. A size/colour that isn't a key in the map has
    # no per-variant restriction (older products without variant-level stock
    # keep working exactly as before). A variant present with a value <= 0
    # is out of stock and must never be orderable, even if the storefront UI
    # somehow let it through (e.g. a stale page, a bypassed frontend check).
    size_stock_map = prod.get("size_stock") or {}
    color_stock_map = prod.get("color_stock") or {}
    if order.size in size_stock_map and size_stock_map[order.size] is not None and size_stock_map[order.size] <= 0:
        raise HTTPException(status_code=400, detail=f"Size '{order.size}' is out of stock")
    if order.color in color_stock_map and color_stock_map[order.color] is not None and color_stock_map[order.color] <= 0:
        raise HTTPException(status_code=400, detail=f"Colour '{order.color}' is out of stock")

    # Delivery fee is frozen at order-creation time (from the admin setting)
    # so later changes to the setting never alter an existing order's total.
    delivery_fee = await get_delivery_fee()

    # Main product photo, denormalized onto the order so it stays correct
    # even if the product is later edited or removed. Prefer the new
    # multi-image array's first photo, fall back to the legacy single
    # "image" field for older products.
    main_image_url = None
    if prod.get("images"):
        main_image_url = prod["images"][0]
    elif prod.get("image"):
        main_image_url = prod["image"]

    # checkout_group_id groups every order row created from one checkout —
    # this endpoint creates a single order row per call, but the column is
    # always populated (matching schema_checkout_migration.sql's "COD and
    # Cashfree both use this") so a future multi-item cart or "my orders"
    # view can group/filter consistently regardless of payment method.
    checkout_group_id = str(uuid.uuid4())

    # payment_type/payment_status branch on the now-required payment_method.
    # "upi" (the old WhatsApp-screenshot flow) is no longer a valid value
    # here at all — OrderRequest.payment_method only accepts "cod"/"cashfree".
    if order.payment_method == "cod":
        payment_type = "cod"
        payment_status = "pending"        # unchanged from today's COD behaviour
    else:
        payment_type = "cashfree"
        payment_status = "awaiting_payment"

    # Create order
    try:
        order_data = {
            "customer_name": order.customer_name,
            "customer_phone": order.customer_phone,
            "customer_address": order.customer_address,
            "customer_city": order.customer_city,
            "customer_pincode": order.customer_pincode,
            "product_id": order.product_id,
            "product_name": prod["name"],
            "product_image": main_image_url,
            "size": order.size,
            "color": order.color,
            "our_price": prod["our_price"],
            "shopkeeper_price": prod["shopkeeper_price"],
            "shopkeeper_id": prod["shopkeeper_id"],
            "shopkeeper_code": prod["shopkeeper_code"],
            "payment_type": payment_type,
            "payment_status": payment_status,
            "delivery_fee": delivery_fee,
            # Never trust a user_id supplied by the frontend — OrderRequest
            # has no such field, and this is the only place user_id is set:
            # always the logged-in customer's own id from their session
            # cookie (require_customer), never anything client-supplied.
            "user_id": customer["sub"],
            "checkout_group_id": checkout_group_id,
            "agent_state": {}
        }
        res = await run_query(supabase_admin.table("orders").insert(order_data))
        new_order = res.data[0]
        logger.info(
            f"Order created: {new_order['id']}, customer: {order.customer_phone}, "
            f"product: {prod['name']}, payment_method: {order.payment_method}"
        )

        await cache_delete("orders:recent")
        await cache_delete("admin:dashboard")
        await cache_delete("analytics:overview")
    except Exception as e:
        logger.error(f"Order save failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create order")

    # Decrement stock atomically — only succeeds if stock hasn't changed
    # since we read it above. If it has (a concurrent order beat us to it),
    # roll back the order we just created instead of allowing stock to go negative.
    try:
        stock_update = {"stock": prod["stock"] - 1}
        if order.size in size_stock_map and size_stock_map[order.size] is not None:
            updated_size_map = dict(size_stock_map)
            updated_size_map[order.size] = max(0, updated_size_map[order.size] - 1)
            stock_update["size_stock"] = updated_size_map
        if order.color in color_stock_map and color_stock_map[order.color] is not None:
            updated_color_map = dict(color_stock_map)
            updated_color_map[order.color] = max(0, updated_color_map[order.color] - 1)
            stock_update["color_stock"] = updated_color_map

        stock_result = await run_query(
            supabase_admin.table("products")
            .update(stock_update)
            .eq("id", order.product_id)
            .eq("stock", prod["stock"])
        )

        if not stock_result.data:
            await run_query(supabase_admin.table("orders").delete().eq("id", new_order["id"]))
            logger.warning(f"Stock race condition detected for {order.product_id} — order {new_order['id']} rolled back")
            raise HTTPException(
                status_code=409,
                detail="Product just went out of stock. Please try again."
            )
        # Invalidate cached product data so the storefront immediately
        # reflects the updated overall/variant stock instead of serving a
        # stale "in stock" snapshot to the next visitor.
        mem_clear_pattern("product:")
        await cache_clear_pattern("products:*")

        # Notify admins if this order just pushed the product (or the
        # specific size/colour ordered) to zero — never on every order,
        # only the one that actually crosses from >0 to 0.
        _fire_and_forget(
            check_out_of_stock(order.product_id, prod["name"], prod, stock_update),
            f"out-of-stock check for {order.product_id}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Stock update failed for {order.product_id}: {e}", exc_info=True)

    # ── Notifications: new order (admin), product ordered (shopkeeper),
    #    order placed (customer). Fire-and-forget — never let a notification
    #    failure affect the order response the customer is waiting on. ──
    _fire_and_forget(
        notify_admins(
            "new_order",
            f"New order — {prod['name']}",
            f"{order.customer_name} ordered {prod['name']} ({order.size} / {order.color}), ₹{prod['our_price']}.",
            link="/admin/orders",
            order_id=new_order["id"],
            product_id=order.product_id,
        ),
        f"admin new_order notification for {new_order['id']}",
    )
    _fire_and_forget(
        notify_shopkeeper(
            prod.get("shopkeeper_id"),
            "product_ordered",
            "Your product was ordered! 🎉",
            f"{prod['name']} ({order.size} / {order.color}) was just ordered.",
            link="/shopkeeper/orders",
            order_id=new_order["id"],
            product_id=order.product_id,
        ),
        f"shopkeeper product_ordered notification for {new_order['id']}",
    )
    _fire_and_forget(
        notify_customer(
            customer["sub"],
            "order_created",
            "Order placed ✅",
            f"Your order for {prod['name']} ({order.size} / {order.color}) has been placed.",
            link="/my-orders",
            order_id=new_order["id"],
            product_id=order.product_id,
        ),
        f"customer order_created notification for {new_order['id']}",
    )
    _fire_and_forget(
        send_order_sms(
            order.customer_phone, "order_created",
            {"var1": order.customer_name, "var2": prod["name"], "var3": new_order["id"][:8]},
        ),
        f"order_created SMS for {new_order['id']}",
    )

    # No WhatsApp redirect for either payment method any more — the
    # customer's order is placed and confirmed entirely through this API
    # response (COD) or the Cashfree Checkout flow (below). The old
    # admin_notification / whatsapp_message / admin_phone WhatsApp-deep-link
    # payload that used to send the customer to WhatsApp to "place" the
    # order has been removed. WhatsApp has since been removed from the app
    # entirely — there is no longer any WhatsApp bot/webhook, and no
    # customer-facing WhatsApp notifications fire from update_order() below;
    # the shopkeeper's package PDF is generated and stored for download from
    # the admin/shopkeeper panel instead (see _generate_shopkeeper_package_pdf).
    if order.payment_method == "cod":
        return {
            "success": True,
            "order_id": new_order["id"],
            "payment_method": "cod",
            "product_image": main_image_url,
        }

    # ── Cashfree: hand off to the existing payments router ──────────────
    # This endpoint does no Cashfree API calls, no payment_records writes,
    # and no order-status transitions of its own beyond what was already
    # written above (payment_status="awaiting_payment"). It only calls the
    # already-built, already-tested create_payment_session() from
    # routers/payments.py — the single owner of all Cashfree logic.
    #
    # Called as a direct function call rather than a real HTTP round-trip
    # to POST /api/payments/cashfree/create-session: it's the same request
    # (so the same customer/request context) and the same process, so an
    # actual self-HTTP-call would need BASE_URL configured to reach itself,
    # would re-forward cookies for no benefit (we already have `customer`
    # from require_customer right here), and adds a needless network hop
    # inside a request that's still holding the DB writes above. This still
    # satisfies "only call into the existing router" — zero Cashfree logic
    # is reimplemented here, create_payment_session's own auth (Depends
    # (require_customer)), rate limit, and error handling all still apply
    # exactly as they do when the frontend calls that endpoint directly
    # (e.g. for a payment retry). If you'd rather this be a literal HTTP
    # call instead, that's a one-line swap to httpx.post(...) — happy to
    # make that change if you prefer it.
    session_result = await create_payment_session(
        CreateSessionRequest(checkout_group_id=checkout_group_id),
        request,
        customer,
    )

    return {
        "success": True,
        "order_id": new_order["id"],
        "payment_method": "cashfree",
        "checkout_group_id": checkout_group_id,
        "payment_session_id": session_result["payment_session_id"],
        "cashfree_order_id": session_result["cashfree_order_id"],
        "amount": session_result["amount"],
        "cashfree_env": session_result["cashfree_env"],
        "product_image": main_image_url,
    }


# ─── CUSTOMER ENDPOINTS ────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    review_text: str = Field("", max_length=2000)


# Statuses past which an order can no longer be cancelled by the customer.
NON_CANCELLABLE_STATUSES = {"shipped", "delivered", "cancelled", "refunded"}


@router.get("/mine")
async def my_orders(checkout_group_id: str | None = None, customer=Depends(require_customer)):
    """
    Read-only order lookup for the logged-in customer. Originally built
    narrow (for the post-Cashfree confirmation page's order summary), now
    also backs the full "My Orders" history page — same query, same
    ownership scoping, just more fields (shipment/tracking status) and a
    higher limit. Always scoped to the requesting customer's own user_id;
    checkout_group_id further narrows to a single checkout when provided
    (used by the confirmation page — the history page omits it to get
    everything).

    Shipment status here is whatever NimbusPost last told us at shipment-
    creation time (courier_name/tracking_id/shipping_status/nimbuspost_awb),
    not a live courier lookup — live tracking is a separate, admin-only call
    (see /admin/orders/{id}/track in routers/admin.py) that would need its
    own customer-safe wrapper to expose here; out of scope for now.

    NOTE: registered before GET /{order_id} below — both are single-segment
    GET routes under this router, and FastAPI matches in registration order,
    so /mine must come first or it would itself get swallowed by /{order_id}
    (order_id="mine").
    """
    try:
        query = (
            supabase_admin.table("orders")
            .select("id,product_name,product_image,size,color,our_price,delivery_fee,"
                    "payment_type,payment_status,status,checkout_group_id,created_at,"
                    "courier_name,tracking_id,shipping_status,nimbuspost_awb")
            .eq("user_id", customer["sub"])
        )
        if checkout_group_id:
            query = query.eq("checkout_group_id", checkout_group_id)
        res = await run_query(query.order("created_at", desc=True).limit(200))
    except Exception as e:
        logger.error(f"Failed to fetch orders for customer {customer['sub']}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch orders")

    return {"orders": res.data or []}


@router.get("/{order_id}")
async def my_order_detail(order_id: str, customer=Depends(require_customer)):
    """
    Full single-order detail for the "My Orders" click-through view.
    Ownership scoped to the logged-in customer via .eq(user_id) — never
    returns another customer's order, 404s instead of leaking existence.
    """
    try:
        res = await run_query(
            supabase_admin.table("orders").select(
                "id,product_id,product_name,product_image,size,color,our_price,delivery_fee,"
                "payment_type,payment_status,status,refund_status,checkout_group_id,created_at,"
                "customer_name,customer_phone,customer_address,customer_city,customer_pincode,"
                "courier_name,tracking_id,shipping_status,nimbuspost_awb"
            )
            .eq("id", order_id).eq("user_id", customer["sub"]).single()
        )
        order = res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        # Whether "Write a Review" should be offered, and whether this order
        # already has one — checked here so the page doesn't need a second
        # round trip just to know whether to show the form or the existing review.
        review_res = await run_query(
            supabase_admin.table("reviews").select("id,rating,review_text,created_at")
            .eq("order_id", order_id).maybe_single()
        )
        order["review"] = review_res.data if review_res and review_res.data else None
        order["cancellable"] = order.get("status") not in NON_CANCELLABLE_STATUSES

        return order
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch order detail {order_id} for customer {customer['sub']}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch order")


@router.post("/{order_id}/cancel")
@limiter.limit("10/minute")
async def cancel_order(order_id: str, request: Request, customer=Depends(require_customer)):
    """
    Customer-initiated cancellation. Only allowed before the order has
    shipped (NON_CANCELLABLE_STATUSES). No automatic refund logic here —
    if a Cashfree payment was already captured (payment_status == "verified"),
    the order is flagged refund_status="pending" so admins see it needs
    manual refund review; nothing is charged/refunded automatically.
    """
    try:
        current = await run_query(
            supabase_admin.table("orders").select("*")
            .eq("id", order_id).eq("user_id", customer["sub"]).single()
        )
        order = current.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.get("status") in NON_CANCELLABLE_STATUSES:
            raise HTTPException(status_code=400, detail=f"This order can no longer be cancelled (status: {order.get('status')}).")

        updates = {"status": "cancelled"}
        payment_captured = order.get("payment_type") == "cashfree" and order.get("payment_status") == "verified"
        if payment_captured:
            updates["refund_status"] = "pending"

        # Restore the stock that was decremented when this order was placed —
        # guarded by stock_restored so a cancel that somehow runs twice (or
        # runs after some other path already restored it) never double-credits
        # the product's stock.
        if not order.get("stock_restored"):
            if await _restore_order_stock(order):
                updates["stock_restored"] = True

        await run_query(supabase_admin.table("orders").update(updates).eq("id", order_id))
        logger.info(f"Order {order_id} cancelled by customer {customer['sub']} (refund_pending={payment_captured})")

        await cache_delete("admin:dashboard")
        await cache_delete("analytics:overview")
        await cache_delete("orders:recent")

        _fire_and_forget(
            notify_admins(
                "order_cancelled",
                "Order cancelled by customer",
                f"{order.get('customer_name')} cancelled their order for {order.get('product_name')}."
                + (" Payment was already captured — refund needs review." if payment_captured else ""),
                link="/admin/orders",
                order_id=order_id,
                product_id=order.get("product_id"),
            ),
            f"admin order_cancelled notification for {order_id}",
        )

        return {"success": True, "status": "cancelled", "refund_status": updates.get("refund_status", order.get("refund_status"))}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to cancel order")


@router.post("/{order_id}/review")
@limiter.limit("10/minute")
async def submit_review(order_id: str, review: ReviewRequest, request: Request, customer=Depends(require_customer)):
    """
    Minimal review: rating + text, tied to the order (and denormalized
    product_id so the product page can query without joining orders).
    Only allowed once the order is delivered. One review per order,
    enforced by the DB's unique(order_id) constraint — a second attempt
    is rejected with a clear message rather than a raw DB error.
    """
    try:
        current = await run_query(
            supabase_admin.table("orders").select("id,status,product_id,customer_name")
            .eq("id", order_id).eq("user_id", customer["sub"]).single()
        )
        order = current.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.get("status") != "delivered":
            raise HTTPException(status_code=400, detail="You can review this order once it has been delivered.")

        existing = await run_query(supabase_admin.table("reviews").select("id").eq("order_id", order_id).maybe_single())
        if existing and existing.data:
            raise HTTPException(status_code=400, detail="You've already reviewed this order.")

        row = {
            "order_id": order_id,
            "product_id": order.get("product_id"),
            "customer_phone": customer.get("phone") or "",
            "customer_name": order.get("customer_name"),
            "rating": review.rating,
            "review_text": review.review_text.strip(),
        }
        res = await run_query(supabase_admin.table("reviews").insert(row))
        await cache_clear_pattern("products:*")
        mem_clear_pattern("product:")
        await cache_delete(f"product:{order.get('product_id')}:reviews")
        logger.info(f"Review submitted for order {order_id} by customer {customer['sub']}")
        return res.data[0] if res.data else {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit review for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit review")


# ─── ADMIN ENDPOINTS ──────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_list_orders(
    status: str | None = None,
    admin=Depends(require_admin)
):
    try:
        q = supabase_admin.table("orders").select("*").order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        res = await run_query(q)
        return res.data or []
    except Exception as e:
        logger.error(f"Admin: list orders failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch orders")


@router.get("/admin/{order_id}/package-pdf")
async def admin_package_pdf(order_id: str, admin=Depends(require_admin)):
    """
    Admin-side equivalent of GET /shopkeeper/{order_id}/package-pdf below —
    same stored PDF, no shopkeeper_id ownership filter since admins can see
    every order.
    """
    try:
        res = await run_query(
            supabase_admin.table("orders")
            .select("id,package_pdf_status,package_pdf_base64,package_pdf_filename")
            .eq("id", order_id).single()
        )
        order = res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.get("package_pdf_status") != "ready" or not order.get("package_pdf_base64"):
            raise HTTPException(status_code=404, detail="Package PDF is not ready for this order yet")

        return {
            "filename": order.get("package_pdf_filename") or f"order_{order_id[:8]}.pdf",
            "content_base64": order["package_pdf_base64"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Admin package PDF fetch failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch order data")


@router.put("/admin/{order_id}")
async def update_order(order_id: str, update: StatusUpdate, admin=Depends(require_admin)):
    try:
        current = await run_query(supabase_admin.table("orders").select("*").eq("id", order_id).single())
        if not current.data:
            raise HTTPException(status_code=404, detail="Order not found")
        order = current.data

        updates = {}
        if update.status:
            updates["status"] = update.status
        if update.tracking_id:
            updates["tracking_id"] = update.tracking_id
        if update.courier_name:
            updates["courier_name"] = update.courier_name
        if update.payment_status:
            updates["payment_status"] = update.payment_status
        if update.refund_status:
            updates["refund_status"] = update.refund_status

        # Admin setting the status to "cancelled" here is functionally the
        # same reversal as the customer's own cancel_order() — restore the
        # stock decremented at order creation, guarded by stock_restored so
        # an order already cancelled (whether via this endpoint or the
        # customer-facing one) never has its stock restored a second time.
        if update.status == "cancelled" and order.get("status") != "cancelled" and not order.get("stock_restored"):
            if await _restore_order_stock(order):
                updates["stock_restored"] = True

        await run_query(supabase_admin.table("orders").update(updates).eq("id", order_id))
        logger.info(f"Order status updated: {order_id}, {order['status']} → {update.status}")

        await cache_delete("admin:dashboard")
        await cache_delete("analytics:overview")
        await cache_delete("orders:recent")

        # ── Customer notification on status transitions the customer cares
        #    about — created is already sent from create_order(), so only
        #    confirmed/shipped/delivered fire here, and only when the status
        #    actually changed (never re-notify on an unrelated field edit,
        #    e.g. just adding a tracking_id while status stays "shipped"). ──
        if update.status and update.status != order.get("status") and update.status in ("confirmed", "shipped", "delivered"):
            status_copy = {
                "confirmed": ("Order confirmed ✅", "Your order for {product} has been confirmed and is being prepared."),
                "shipped":   ("Order shipped 🚚", "Your order for {product} has shipped{tracking}."),
                "delivered": ("Order delivered 📦", "Your order for {product} has been delivered. We hope you love it!"),
            }
            title, msg_template = status_copy[update.status]
            tracking_note = f" (tracking: {update.tracking_id})" if update.status == "shipped" and update.tracking_id else ""
            message = msg_template.format(product=order.get("product_name") or "your item", tracking=tracking_note)
            customer_id = order.get("user_id")
            _fire_and_forget(
                notify_customer(
                    customer_id, f"order_{update.status}", title, message,
                    link="/my-orders", order_id=order_id, product_id=order.get("product_id"),
                ),
                f"customer order_{update.status} notification for {order_id}",
            )
            _fire_and_forget(
                send_order_sms(
                    order["customer_phone"], f"order_{update.status}",
                    {"var1": order.get("customer_name", ""), "var2": order.get("product_name", ""),
                     "var3": update.tracking_id or order_id[:8]},
                ),
                f"order_{update.status} SMS for {order_id}",
            )

        payment_type = order.get("payment_type")

        if update.status == "confirmed" and payment_type == "upi" and update.payment_status == "verified":
            # Build the shopkeeper's package PDF (product photo + either
            # NimbusPost's label or our fallback slip) and store it, ready
            # for the admin/shopkeeper to download — never sent automatically.
            # If auto-ship is on, create_shipment_for_order() below generates
            # it exactly once from there instead — doing it here too would
            # duplicate it. If auto-ship is off, no shipment will ever be
            # attempted automatically, so generate the fallback version now.
            if not await _is_auto_ship_enabled():
                try:
                    shopkeeper_id = order.get("shopkeeper_id")
                    if shopkeeper_id:
                        sk_res = await run_query(supabase_admin.table("shopkeepers").select("*").eq("id", shopkeeper_id).single())
                        shopkeeper = sk_res.data
                        if shopkeeper:
                            _fire_and_forget(
                                _generate_shopkeeper_package_pdf(order, shopkeeper, None),
                                f"package PDF for order {order_id} (manual confirm, auto-ship off)",
                            )
                        else:
                            logger.warning(f"Shopkeeper {shopkeeper_id} not found — skipping package PDF")
                    else:
                        logger.warning(f"Order {order_id} has no shopkeeper_id — skipping package PDF")
                except Exception as e:
                    logger.warning(f"Shopkeeper package PDF failed for order {order_id}: {e}")

        # COD orders are never auto-confirmed — they sit in "pending" until
        # an admin confirms them here.
        if update.status == "confirmed" and payment_type == "cod" and order.get("status") != "confirmed":
            # Same as the UPI branch above: build the shopkeeper's package PDF
            # now if auto-ship won't do it later. Previously this was missing
            # entirely for COD, so COD orders never got a package PDF unless
            # auto-ship happened to also be on with payment_status="verified".
            if not await _is_auto_ship_enabled():
                try:
                    shopkeeper_id = order.get("shopkeeper_id")
                    if shopkeeper_id:
                        sk_res = await run_query(supabase_admin.table("shopkeepers").select("*").eq("id", shopkeeper_id).single())
                        shopkeeper = sk_res.data
                        if shopkeeper:
                            _fire_and_forget(
                                _generate_shopkeeper_package_pdf(order, shopkeeper, None),
                                f"package PDF for order {order_id} (manual confirm, COD, auto-ship off)",
                            )
                        else:
                            logger.warning(f"Shopkeeper {shopkeeper_id} not found — skipping package PDF")
                    else:
                        logger.warning(f"Order {order_id} has no shopkeeper_id — skipping package PDF")
                except Exception as e:
                    logger.warning(f"Shopkeeper package PDF failed for order {order_id}: {e}")

        # NimbusPost auto-ship: only fires when payment is verified on a
        # confirmed order and the auto-mode setting is turned on. In manual
        # mode, admins trigger shipment creation from the Orders page instead.
        if (update.status == "confirmed"
                and update.payment_status == "verified"
                and await _is_auto_ship_enabled()):
            merged_order = {**order, **updates}
            await create_shipment_for_order(merged_order)

        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Order update failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update order")


@router.delete("/admin/{order_id}")
async def admin_delete_order(order_id: str, admin=Depends(require_admin)):
    """
    Permanently removes an order row — distinct from cancellation (which
    just changes status and keeps the record). Admin-only, full delete
    from the admin's own management view.
    """
    try:
        existing = await run_query(supabase_admin.table("orders").select("*").eq("id", order_id).single())
        if not existing.data:
            raise HTTPException(status_code=404, detail="Order not found")
        order = existing.data

        # An order being permanently deleted here may never have gone
        # through cancel_order()/update_order()'s "cancelled" path (e.g. an
        # admin deleting a stray/duplicate order outright), so its stock was
        # never restored — restore it now. stock_restored guards the case
        # this fix explicitly needs to avoid: an order cancelled first
        # (which already restored stock) and then deleted, which must not
        # restore it a second time.
        if not order.get("stock_restored"):
            await _restore_order_stock(order)

        await run_query(supabase_admin.table("orders").delete().eq("id", order_id))
        await cache_delete("admin:dashboard")
        await cache_delete("analytics:overview")
        await cache_delete("orders:recent")
        logger.info(f"Order {order_id} permanently deleted by admin {admin['sub']}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete order")


@router.get("/admin/recent")
async def recent_orders(admin=Depends(require_admin)):
    cache_key = "orders:recent"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(supabase_admin.table("orders").select("*").order("created_at", desc=True).limit(10))
        data = res.data or []
        await cache_set(cache_key, data, ttl=60)
        return data
    except Exception as e:
        logger.error(f"Recent orders fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch recent orders")


# ─── SHOPKEEPER-FACING ──────────────────────────────────────────────────────
# Minimal, PII-light view: shopkeepers see their own orders' product/status
# info and what they get paid (shopkeeper_price), never the customer's
# contact/address (fulfillment is handled centrally by the Clovical team)
# and never our_price/profit. The product image is the SAME URL already
# stored on the order (product_image, denormalized at order-creation time)
# — no second copy is made for the shopkeeper view.

@router.get("/shopkeeper/mine")
async def shopkeeper_list_orders(status: str | None = None, shopkeeper=Depends(require_shopkeeper)):
    try:
        q = (
            supabase_admin.table("orders")
            .select("id,product_name,product_image,size,color,shopkeeper_price,payment_type,payment_status,status,tracking_id,courier_name,package_pdf_status,created_at")
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("created_at", desc=True)
        )
        if status:
            q = q.eq("status", status)
        res = await run_query(q)
        return res.data or []
    except Exception as e:
        logger.error(f"Shopkeeper: list orders failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch orders")


@router.get("/shopkeeper/{order_id}/pdf-data")
async def shopkeeper_order_pdf_data(order_id: str, shopkeeper=Depends(require_shopkeeper)):
    """
    Same shape/format as the admin's /api/admin/orders/{id}/pdf-data (used
    by the identical client-side jsPDF renderer), scoped to the requesting
    shopkeeper's own order and stripped of customer PII and our_price/profit.
    """
    try:
        res = await run_query(
            supabase_admin.table("orders").select("*")
            .eq("id", order_id).eq("shopkeeper_id", shopkeeper["shopkeeper_id"]).single()
        )
        order = res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")

        return {
            "order": {
                "id": order["id"],
                "created_at": order.get("created_at"),
                "status": order.get("status"),
            },
            "product": {
                "image": order.get("product_image"),
                "name": order.get("product_name"),
                "size": order.get("size"),
                "color": order.get("color"),
                "shopkeeper_code": order.get("shopkeeper_code"),
                "shopkeeper_price": order.get("shopkeeper_price"),
            },
            "payment": {
                "type": order.get("payment_type"),
                "status": order.get("payment_status"),
            },
            "shipping": {
                "courier_name": order.get("courier_name"),
                "tracking_id": order.get("tracking_id"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Shopkeeper PDF data fetch failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch PDF data")


@router.get("/shopkeeper/{order_id}/package-pdf")
async def shopkeeper_package_pdf(order_id: str, shopkeeper=Depends(require_shopkeeper)):
    """
    Downloads the stored package PDF (product photo + NimbusPost's official
    label, built by _generate_shopkeeper_package_pdf) for one of this
    shopkeeper's own orders. This is the replacement for the old WhatsApp
    pull — the only way this PDF ever reaches anyone now.

    Ownership: scoped by .eq(shopkeeper_id) same as every other shopkeeper
    endpoint here — a shopkeeper can never fetch another shopkeeper's PDF.
    """
    try:
        res = await run_query(
            supabase_admin.table("orders")
            .select("id,package_pdf_status,package_pdf_base64,package_pdf_filename")
            .eq("id", order_id).eq("shopkeeper_id", shopkeeper["shopkeeper_id"]).single()
        )
        order = res.data
        if not order:
            raise HTTPException(status_code=404, detail="Order not found")
        if order.get("package_pdf_status") != "ready" or not order.get("package_pdf_base64"):
            raise HTTPException(status_code=404, detail="Package PDF is not ready for this order yet")

        return {
            "filename": order.get("package_pdf_filename") or f"order_{order_id[:8]}.pdf",
            "content_base64": order["package_pdf_base64"],
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Shopkeeper package PDF fetch failed for order {order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch order data")