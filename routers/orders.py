import re
import asyncio
import base64
import logging
import requests
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import require_admin
from utils.captcha import verify_turnstile
from utils.whatsapp_utils import (
    send_text, send_image_url, send_upi_qr,
    msg_order_received, msg_shipped, msg_refund_processed,
    msg_payment_confirmed, msg_cod_confirmed, msg_track_delivered,
)
from utils.nimbuspost import create_shipment
from utils.label_generator import build_shopkeeper_package_pdf
from utils.cache import cache_get, cache_set, cache_delete, two_layer_get, two_layer_set, mem_clear_pattern, cache_clear_pattern

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

WA_NUMBER = __import__("os").getenv("WHATSAPP_NUMBER", "")


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


async def _mark_package_pdf_status(order_id: str, status: str):
    """
    Records the shopkeeper package PDF's state on the order. `status` is one
    of:
      'ready'  — PDF built and stored, waiting for the shopkeeper to request it
      'sent'   — delivered to the shopkeeper over WhatsApp
      'failed' — PDF generation itself failed (not a send failure — we no
                 longer send automatically, see _generate_shopkeeper_package_pdf)
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
    it does NOT send it over WhatsApp.

    Why: this used to push the PDF to the shopkeeper's WhatsApp the instant
    an order was confirmed/shipped. That's a business-initiated message the
    shopkeeper never asked for in that moment, and doing it automatically on
    every single order is exactly the pattern WhatsApp's spam heuristics
    flag — which is what got the number temporarily restricted. Automatic
    pushes are gone for good now.

    Instead, the PDF sits here (base64, package_pdf_status='ready') until
    the shopkeeper's own registered WhatsApp number messages the bot asking
    for their orders. That delivery — a reply to an incoming message, not a
    cold push — is handled by handle_shopkeeper_order_pull() in
    routers/whatsapp.py, and is the ONLY place a package PDF is ever sent.

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
    shipment, then persists the returned AWB/courier/label on the order
    and notifies the customer via WhatsApp.

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
            # photo + order details ready to pull whenever they ask for it.
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

        # From here on, everything is notification/best-effort work that
        # shouldn't hold up the caller (the admin's "Ship" click, or the
        # auto-ship trigger) now that the shipment itself is confirmed
        # created. Runs in the background; failures are logged, not raised.
        tracking_url = f"https://www.nimbuspost.com/track/{result['awb']}"

        async def _notify_and_send_package_pdf():
            await run_blocking(
                send_text, order["customer_phone"],
                msg_shipped(order["product_name"], result["courier_name"] or "Courier", result["awb"], tracking_url)
            )

            # Fetch NimbusPost's own official label and merge it (untouched —
            # never edited, so the barcode/AWB stays valid) with our product
            # photo page, then build+store the combined PDF for the
            # shopkeeper to pull later — not send it now.
            label_bytes = None
            if result.get("label_url"):
                try:
                    label_resp = await run_blocking(requests.get, result["label_url"], timeout=15)
                    label_resp.raise_for_status()
                    label_bytes = label_resp.content
                except Exception as e:
                    logger.warning(f"Could not fetch NimbusPost label PDF for order {order_id}: {e}")
            await _generate_shopkeeper_package_pdf(order, shopkeeper, label_bytes)

        _fire_and_forget(_notify_and_send_package_pdf(), f"post-shipment notifications for order {order_id}")

        return result
    except Exception as e:
        logger.error(f"create_shipment_for_order failed for order {order_id}: {e}", exc_info=True)
        await _release_shipment_claim(order_id, "failed")
        return None


@router.post("/create")
@limiter.limit("5/minute")
async def create_order(order: OrderRequest, request: Request):
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
            "payment_type": "upi",
            "delivery_fee": delivery_fee,
            "agent_state": {}
        }
        res = await run_query(supabase_admin.table("orders").insert(order_data))
        new_order = res.data[0]
        logger.info(f"Order created: {new_order['id']}, customer: {order.customer_phone}, product: {prod['name']}")

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
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Stock update failed for {order.product_id}: {e}", exc_info=True)

    total_amount = prod["our_price"] + delivery_fee
    price_lines = (
        f"Price: ₹{prod['our_price']:.0f}\n"
        f"Delivery: ₹{delivery_fee:.0f}\n"
        f"Total: ₹{total_amount:.0f}\n\n"
    ) if delivery_fee else f"Price: ₹{prod['our_price']:.0f}\n\n"

    admin_notification = (
        f"🛍️ New Order!\n\n"
        f"Product: {prod['name']}\n"
        f"Code: {prod['shopkeeper_code']}\n"
        f"Size: {order.size} | Color: {order.color}\n"
        f"{price_lines}"
        f"👤 Customer Details:\n"
        f"Name: {order.customer_name}\n"
        f"Phone: {order.customer_phone}\n"
        f"Address: {order.customer_address}\n"
        f"City: {order.customer_city}\n"
        f"Pincode: {order.customer_pincode}"
    )

    return {
        "success": True,
        "order_id": new_order["id"],
        "admin_phone": WA_NUMBER,
        "whatsapp_message": admin_notification,
        "product_image": main_image_url
    }


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

        await run_query(supabase_admin.table("orders").update(updates).eq("id", order_id))
        logger.info(f"Order status updated: {order_id}, {order['status']} → {update.status}")

        await cache_delete("admin:dashboard")
        await cache_delete("analytics:overview")
        await cache_delete("orders:recent")

        # WhatsApp notifications on status change
        phone = order["customer_phone"]

        if update.status == "shipped" and update.tracking_id:
            courier = update.courier_name or "Courier"
            tracking_url = f"https://www.delhivery.com/track/package/{update.tracking_id}"
            await run_blocking(send_text, phone, msg_shipped(order["product_name"], courier, update.tracking_id, tracking_url))

        delivery_fee = order.get("delivery_fee") or 0
        total_amount = order.get("total_amount") or (order["our_price"] + delivery_fee)

        payment_type = order.get("payment_type")

        if update.status == "confirmed" and payment_type == "upi" and update.payment_status == "verified":
            await run_blocking(
                send_text, phone,
                msg_payment_confirmed(order["product_name"], order["size"], order["color"], total_amount, delivery_fee)
            )

            # Build the shopkeeper's package PDF (product photo + either
            # NimbusPost's label or our fallback slip) and store it, ready
            # for the shopkeeper to pull — never sent automatically. If
            # auto-ship is on, create_shipment_for_order() below generates
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

        # COD orders are never auto-confirmed by the WhatsApp bot (see
        # routers/whatsapp.py) — they sit in "pending" until an admin
        # confirms them here. That confirmation is what should fire the
        # "Order Confirmed (COD)" message; previously this branch only
        # matched payment_type == "upi", so COD customers never got a
        # confirmation message at all.
        if update.status == "confirmed" and payment_type == "cod" and order.get("status") != "confirmed":
            await run_blocking(
                send_text, phone,
                msg_cod_confirmed(order["product_name"], order["size"], order["color"], total_amount, delivery_fee)
            )

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

        # Delivered status had no WhatsApp trigger at all — admins marking an
        # order "delivered" produced no customer-facing message.
        if update.status == "delivered" and order.get("status") != "delivered":
            await run_blocking(send_text, phone, msg_track_delivered())

        if update.refund_status == "processed":
            await run_blocking(send_text, phone, msg_refund_processed(total_amount))

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