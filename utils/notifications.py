"""
utils/notifications.py — In-app notification helper
=====================================================
Thin write-side helper used by other routers to create notification rows.
Every function here is deliberately best-effort and NEVER raises — a
notification failing to write must never break the order/request/stock
operation that triggered it. Failures are logged and swallowed.

Recipient model (see schema_notifications.sql):
  • admin      → recipient_id NULL, broadcast to every admin session
  • shopkeeper → recipient_id = shopkeepers.id (str)
  • customer   → recipient_id = customers.id   (str/uuid)
"""
import asyncio
import logging
from utils.db import supabase_admin, run_query
from utils.cache import cache_clear_pattern
from utils.fcm_push import send_push

logger = logging.getLogger(__name__)


def _fire_push(recipient_type: str, recipient_id, title: str, message: str, link: str | None):
    """
    Schedules the FCM push as a background task so it never adds latency
    to a notification write — several call sites (e.g. routers/requests.py)
    `await notify_admins(...)` directly rather than fire-and-forgetting it,
    and a slow/unconfigured FCM call must not make those awaits any slower
    than they already are. send_push() itself never raises; this wrapper
    just makes sure a task-creation failure (e.g. no running loop, which
    shouldn't happen here but cost nothing to guard) can't take down the
    in-app notification write either.
    """
    try:
        task = asyncio.create_task(send_push(recipient_type, recipient_id, title, message, link))

        def _log_if_failed(t: asyncio.Task):
            exc = t.exception() if not t.cancelled() else None
            if exc:
                logger.error(f"Push send failed ({recipient_type}/{recipient_id}): {exc}", exc_info=exc)

        task.add_done_callback(_log_if_failed)
    except Exception as e:
        logger.error(f"Could not schedule push send ({recipient_type}/{recipient_id}): {e}", exc_info=True)


async def _insert(recipient_type: str, recipient_id, type_: str, title: str,
                   message: str = "", link: str | None = None,
                   order_id: str | None = None, request_id: str | None = None,
                   product_id: str | None = None):
    try:
        row = {
            "recipient_type": recipient_type,
            "recipient_id": str(recipient_id) if recipient_id is not None else None,
            "type": type_,
            "title": title,
            "message": message or "",
            "link": link,
            "order_id": order_id,
            "request_id": request_id,
            "product_id": product_id,
        }
        await run_query(supabase_admin.table("notifications").insert(row))
        # Bust the cached unread-count for whoever should see this immediately.
        if recipient_type == "admin":
            await cache_clear_pattern("notif:admin:*")
        else:
            await cache_clear_pattern(f"notif:{recipient_type}:{recipient_id}:*")
        # Real push (browser/phone popup) alongside the in-app row above —
        # additive only, never blocks and never affects the write above.
        _fire_push(recipient_type, recipient_id, title, message, link)
    except Exception as e:
        logger.error(f"Failed to create notification ({recipient_type}/{recipient_id}, {type_}): {e}", exc_info=True)


async def notify_admins(type_: str, title: str, message: str = "", link: str | None = None,
                         order_id: str | None = None, request_id: str | None = None,
                         product_id: str | None = None):
    await _insert("admin", None, type_, title, message, link, order_id, request_id, product_id)


async def notify_shopkeeper(shopkeeper_id, type_: str, title: str, message: str = "", link: str | None = None,
                             order_id: str | None = None, request_id: str | None = None,
                             product_id: str | None = None):
    if not shopkeeper_id:
        return
    await _insert("shopkeeper", shopkeeper_id, type_, title, message, link, order_id, request_id, product_id)


async def notify_customer(customer_id, type_: str, title: str, message: str = "", link: str | None = None,
                           order_id: str | None = None, request_id: str | None = None,
                           product_id: str | None = None):
    if not customer_id:
        return
    await _insert("customer", customer_id, type_, title, message, link, order_id, request_id, product_id)


async def check_out_of_stock(product_id: str, product_name: str, before: dict, after: dict):
    """
    Notifies admins exactly once per stock-crossing-to-zero event — never
    on every save where a variant is already sitting at 0. `before`/`after`
    are dicts that may contain 'stock' (int), 'size_stock' (dict), and
    'color_stock' (dict); missing keys are treated as "not tracked, skip".
    """
    try:
        newly_out = []

        b_stock, a_stock = before.get("stock"), after.get("stock")
        if b_stock is not None and a_stock is not None and b_stock > 0 and a_stock <= 0:
            newly_out.append("Overall stock")

        for field, label in (("size_stock", "Size"), ("color_stock", "Colour")):
            b_map = before.get(field) or {}
            a_map = after.get(field) or {}
            for key, a_val in a_map.items():
                b_val = b_map.get(key)
                if b_val is not None and a_val is not None and b_val > 0 and a_val <= 0:
                    newly_out.append(f"{label} '{key}'")

        if newly_out:
            details = ", ".join(newly_out)
            await notify_admins(
                "out_of_stock",
                f"Out of stock — {product_name}",
                f"{details} just ran out for '{product_name}'.",
                link="/admin/stock",
                product_id=product_id,
            )
    except Exception as e:
        logger.error(f"check_out_of_stock failed for {product_id}: {e}", exc_info=True)