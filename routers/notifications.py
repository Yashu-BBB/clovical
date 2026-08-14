"""
routers/notifications.py — Notification read API
====================================================
Read/mark-read endpoints for the three notification inboxes (admin,
shopkeeper, customer). Writes happen exclusively via utils/notifications.py,
called from the routers where the underlying event actually occurs (order
creation, status updates, request submit/accept/reject, stock updates) —
this router is read/ack only.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from utils.db import supabase_admin, run_query
from utils.auth_utils import require_admin, require_shopkeeper, require_customer
from utils.cache import cache_get, cache_set, cache_clear_pattern
from utils.fcm_push import register_token

logger = logging.getLogger(__name__)
router = APIRouter()

LIST_LIMIT = 30


class RegisterTokenRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=4096)


async def _list_for(recipient_type: str, recipient_id, unread_only: bool):
    q = (
        supabase_admin.table("notifications")
        .select("*")
        .eq("recipient_type", recipient_type)
        .order("created_at", desc=True)
        .limit(LIST_LIMIT)
    )
    if recipient_type == "admin":
        q = q.is_("recipient_id", "null")
    else:
        q = q.eq("recipient_id", str(recipient_id))
    if unread_only:
        q = q.eq("is_read", False)
    res = await run_query(q)
    return res.data or []


async def _unread_count(recipient_type: str, recipient_id) -> int:
    cache_key = f"notif:{recipient_type}:{recipient_id}:unread-count"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached
    q = (
        supabase_admin.table("notifications")
        .select("id", count="exact")
        .eq("recipient_type", recipient_type)
        .eq("is_read", False)
    )
    if recipient_type == "admin":
        q = q.is_("recipient_id", "null")
    else:
        q = q.eq("recipient_id", str(recipient_id))
    res = await run_query(q)
    count = res.count or 0
    await cache_set(cache_key, count, ttl=15)
    return count


async def _mark_read(notif_id: str, recipient_type: str, recipient_id):
    q = supabase_admin.table("notifications").update({"is_read": True}).eq("id", notif_id).eq("recipient_type", recipient_type)
    q = q.is_("recipient_id", "null") if recipient_type == "admin" else q.eq("recipient_id", str(recipient_id))
    res = await run_query(q)
    if not res.data:
        raise HTTPException(status_code=404, detail="Notification not found")
    await cache_clear_pattern(f"notif:{recipient_type}:{recipient_id}:*")
    return res.data[0]


async def _mark_all_read(recipient_type: str, recipient_id):
    q = supabase_admin.table("notifications").update({"is_read": True}).eq("recipient_type", recipient_type).eq("is_read", False)
    q = q.is_("recipient_id", "null") if recipient_type == "admin" else q.eq("recipient_id", str(recipient_id))
    await run_query(q)
    await cache_clear_pattern(f"notif:{recipient_type}:{recipient_id}:*")
    return {"success": True}


# ─── ADMIN ──────────────────────────────────────────────────────────────
@router.get("/admin")
async def admin_notifications(unread_only: bool = False, admin=Depends(require_admin)):
    try:
        return await _list_for("admin", None, unread_only)
    except Exception as e:
        logger.error(f"Failed to list admin notifications: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch notifications")

@router.get("/admin/unread-count")
async def admin_unread_count(admin=Depends(require_admin)):
    return {"count": await _unread_count("admin", None)}

@router.put("/admin/{notif_id}/read")
async def admin_mark_read(notif_id: str, admin=Depends(require_admin)):
    return await _mark_read(notif_id, "admin", None)

@router.put("/admin/read-all")
async def admin_mark_all_read(admin=Depends(require_admin)):
    return await _mark_all_read("admin", None)


# ─── SHOPKEEPER ─────────────────────────────────────────────────────────
@router.get("/shopkeeper")
async def shopkeeper_notifications(unread_only: bool = False, shopkeeper=Depends(require_shopkeeper)):
    try:
        return await _list_for("shopkeeper", shopkeeper["shopkeeper_id"], unread_only)
    except Exception as e:
        logger.error(f"Failed to list shopkeeper notifications: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch notifications")

@router.get("/shopkeeper/unread-count")
async def shopkeeper_unread_count(shopkeeper=Depends(require_shopkeeper)):
    return {"count": await _unread_count("shopkeeper", shopkeeper["shopkeeper_id"])}

@router.put("/shopkeeper/{notif_id}/read")
async def shopkeeper_mark_read(notif_id: str, shopkeeper=Depends(require_shopkeeper)):
    return await _mark_read(notif_id, "shopkeeper", shopkeeper["shopkeeper_id"])

@router.put("/shopkeeper/read-all")
async def shopkeeper_mark_all_read(shopkeeper=Depends(require_shopkeeper)):
    return await _mark_all_read("shopkeeper", shopkeeper["shopkeeper_id"])


# ─── CUSTOMER ───────────────────────────────────────────────────────────
@router.get("/customer")
async def customer_notifications(unread_only: bool = False, customer=Depends(require_customer)):
    try:
        return await _list_for("customer", customer["sub"], unread_only)
    except Exception as e:
        logger.error(f"Failed to list customer notifications: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch notifications")

@router.get("/customer/unread-count")
async def customer_unread_count(customer=Depends(require_customer)):
    return {"count": await _unread_count("customer", customer["sub"])}

@router.put("/customer/{notif_id}/read")
async def customer_mark_read(notif_id: str, customer=Depends(require_customer)):
    return await _mark_read(notif_id, "customer", customer["sub"])

@router.put("/customer/read-all")
async def customer_mark_all_read(customer=Depends(require_customer)):
    return await _mark_all_read("customer", customer["sub"])


# ─── PUSH (FCM) — register a device token, one endpoint per panel ───────
# Read-only session deps are reused unchanged from above; write goes to
# push_subscriptions (schema_push_notifications.sql), a separate table from
# `notifications`. This never touches the in-app notification system.

async def _register(recipient_type: str, recipient_id, body: RegisterTokenRequest, request: Request):
    try:
        await register_token(recipient_type, recipient_id, body.token, request.headers.get("user-agent"))
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to register push token ({recipient_type}/{recipient_id}): {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to register for push notifications")


@router.post("/admin/register-token")
async def admin_register_token(body: RegisterTokenRequest, request: Request, admin=Depends(require_admin)):
    return await _register("admin", None, body, request)


@router.post("/shopkeeper/register-token")
async def shopkeeper_register_token(body: RegisterTokenRequest, request: Request, shopkeeper=Depends(require_shopkeeper)):
    return await _register("shopkeeper", shopkeeper["shopkeeper_id"], body, request)


@router.post("/customer/register-token")
async def customer_register_token(body: RegisterTokenRequest, request: Request, customer=Depends(require_customer)):
    return await _register("customer", customer["sub"], body, request)