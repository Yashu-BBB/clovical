"""
routers/payments.py — Cashfree Payment Gateway integration
============================================================
Server-side Cashfree Checkout integration for website-native online
payments (as opposed to the legacy WhatsApp/UPI-screenshot flow, which
is untouched by this file).

Built against Cashfree's Payment Gateway REST API, version 2026-01-01
(current as of writing — confirmed via https://www.cashfree.com/docs).
No Cashfree SDK dependency: the API surface used here (Create Order,
Get Payments for Order, webhook signature verification) is small
enough that plain httpx calls are simpler to audit than pulling in
the `cashfree_pg` package, and this matches the codebase's existing
convention of talking to external gateways (NimbusPost, MSG91,
Google) via raw HTTP rather than vendor SDKs.

Endpoints
─────────
  POST /api/payments/cashfree/create-session
      Logged-in customer only. Looks up the orders in a
      checkout_group_id (must belong to the requesting customer),
      creates a Cashfree order server-side, and returns the
      payment_session_id the frontend needs to open Cashfree Checkout.
      A payment_records row (status PENDING) is written before the
      session ID is ever returned to the browser.

  POST /api/payments/cashfree/webhook
      Public — called by Cashfree, not the browser. Verifies the
      x-webhook-signature/x-webhook-timestamp headers against the raw
      request body, then updates payment_records + orders. This is
      the ONLY place an order is ever marked as paid — a customer
      landing back on return_url proves nothing by itself.

  GET  /api/payments/cashfree/status/{checkout_group_id}
      Logged-in customer only. Read-only lookup of the current
      payment_records status for a checkout group, so the frontend
      can show "checking payment status..." after the Cashfree
      redirect while waiting for the webhook to land. This endpoint
      NEVER marks anything as paid — it only reads what the webhook
      handler has already written.

  POST /api/payments/cashfree/reconcile/{cashfree_order_id}
      Admin only. Manually re-fetches payment status from Cashfree's
      own API (Get Payments for Order) and reconciles our DB — a
      fallback for the rare case a webhook delivery is lost entirely
      (Cashfree retries failed webhooks, but this covers the gap).

Design notes
────────────
• Auth: signature verification uses CASHFREE_SECRET_KEY (the same
  x-client-secret used for API calls) per Cashfree's own docs — there
  is no separate "webhook secret" credential to configure. See
  verify_cashfree_webhook_signature() below.
• Idempotency: payment_records.cashfree_order_id has a unique DB
  constraint (see schema_checkout_migration.sql). The webhook handler
  additionally does a conditional UPDATE keyed on the row's current
  payment_status, so two overlapping deliveries of the same event
  can't both trigger the "mark orders paid" side effect.
• checkout_group_id / orders.user_id: this router reads and writes
  these columns but does NOT populate them at order-creation time —
  that's routers/orders.py's create_order(), which is explicitly out
  of scope for this change. Until checkout_group_id/user_id are wired
  into order creation elsewhere, create-session has no orders to
  attach a payment to; this is expected, not a bug here.
• WhatsApp confirmation messages, shopkeeper package-PDF generation,
  and NimbusPost auto-ship are NOT triggered from this webhook. Those
  live in routers/orders.py's admin status-update flow and are
  intentionally left alone — this file only ever writes
  payment_status/status columns, mirroring exactly what that flow
  would set for a manually-confirmed prepaid order.
"""
import os
import json
import uuid
import hmac
import hashlib
import base64
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address

from utils.db import supabase_admin, run_query
from utils.auth_utils import require_customer, require_admin
from utils.cache import cache_delete

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

# ── Environment ──────────────────────────────────────────────────────────
CASHFREE_APP_ID     = os.getenv("CASHFREE_APP_ID", "")       # x-client-id
CASHFREE_SECRET_KEY = os.getenv("CASHFREE_SECRET_KEY", "")   # x-client-secret
CASHFREE_ENV        = os.getenv("CASHFREE_ENV", "SANDBOX").strip().upper()  # SANDBOX | PRODUCTION
CASHFREE_API_VERSION = os.getenv("CASHFREE_API_VERSION", "2026-01-01")

CASHFREE_BASE_URL = (
    "https://api.cashfree.com/pg" if CASHFREE_ENV == "PRODUCTION"
    else "https://sandbox.cashfree.com/pg"
)

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
# Cashfree substitutes the literal "{order_id}" placeholder with the real
# order_id when it redirects the customer back. This return_url is display
# convenience ONLY — see module docstring, the redirect is never treated
# as payment proof.
CASHFREE_RETURN_URL = os.getenv(
    "CASHFREE_RETURN_URL",
    f"{BASE_URL}/checkout?cf_order_id={{order_id}}"
)
CASHFREE_NOTIFY_URL = os.getenv(
    "CASHFREE_NOTIFY_URL",
    f"{BASE_URL}/api/payments/cashfree/webhook"
)

_REQUEST_TIMEOUT = 15.0

# Cashfree's payment_status values -> payment_records.payment_status
# (payment_records only allows PENDING | SUCCESS | FAILED | CANCELLED |
# USER_DROPPED — see schema_checkout_migration.sql).
_STATUS_MAP = {
    "SUCCESS": "SUCCESS",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
    "USER_DROPPED": "USER_DROPPED",
    "PENDING": "PENDING",
    "NOT_ATTEMPTED": "PENDING",
    "VOID": "FAILED",
}


def _cashfree_configured() -> bool:
    return bool(CASHFREE_APP_ID and CASHFREE_SECRET_KEY)


def _cf_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET_KEY,
        "x-api-version": CASHFREE_API_VERSION,
    }


def _map_cf_status(cf_status: str) -> str:
    mapped = _STATUS_MAP.get((cf_status or "").upper())
    if not mapped:
        logger.warning(f"Unrecognized Cashfree payment_status '{cf_status}' — treating as PENDING")
        return "PENDING"
    return mapped


# ═══════════════════════════════════════════════════════════════════
# 1. Session creation — POST /orders on Cashfree
# ═══════════════════════════════════════════════════════════════════

async def create_cashfree_order(
    cf_order_id: str,
    amount: float,
    customer_details: dict,
    order_tags: dict,
) -> dict:
    """
    Creates an order on Cashfree and returns the raw JSON response
    (contains payment_session_id, cf_order_id, order_status, ...).
    Raises HTTPException on any failure — this is only ever called
    from inside the create-session request path, so surfacing the
    error directly to that caller is correct.
    """
    payload = {
        "order_id": cf_order_id,
        "order_amount": round(float(amount), 2),
        "order_currency": "INR",
        "customer_details": customer_details,
        "order_meta": {
            "return_url": CASHFREE_RETURN_URL,
            "notify_url": CASHFREE_NOTIFY_URL,
        },
        "order_tags": order_tags,
    }
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(
                f"{CASHFREE_BASE_URL}/orders",
                json=payload,
                headers=_cf_headers(),
            )
    except Exception as e:
        logger.error(f"Cashfree create-order request failed for {cf_order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Could not reach payment gateway. Please try again.")

    if resp.status_code != 200:
        logger.error(
            f"Cashfree create-order failed for {cf_order_id}: "
            f"{resp.status_code} {resp.text[:500]}"
        )
        raise HTTPException(status_code=502, detail="Payment gateway rejected the order. Please try again.")

    data = resp.json()
    if not data.get("payment_session_id"):
        logger.error(f"Cashfree create-order returned no payment_session_id for {cf_order_id}: {data}")
        raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")
    return data


# ═══════════════════════════════════════════════════════════════════
# 2. Webhook signature verification
# ═══════════════════════════════════════════════════════════════════

def verify_cashfree_webhook_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """
    Per Cashfree's docs (Webhook Signature Verification):
        signedPayload := timestamp + raw_body
        expectedSignature := Base64Encode(HMACSHA256(signedPayload, secretKey))
    `secretKey` is the same CASHFREE_SECRET_KEY (x-client-secret) used
    for API calls — Cashfree does not issue a separate webhook secret.
    Must be computed over the exact raw bytes Cashfree sent, before any
    JSON parsing (parsing can normalize numeric formatting and break
    the signature — see Cashfree's warning on this).
    """
    if not (timestamp and signature and CASHFREE_SECRET_KEY):
        return False
    try:
        message = timestamp.encode("utf-8") + raw_body
        computed = base64.b64encode(
            hmac.new(CASHFREE_SECRET_KEY.encode("utf-8"), message, hashlib.sha256).digest()
        ).decode("utf-8")
        return hmac.compare_digest(computed, signature)
    except Exception as e:
        logger.error(f"Webhook signature computation failed: {e}", exc_info=True)
        return False


# ═══════════════════════════════════════════════════════════════════
# 3. Server-side payment verification — GET /orders/{id}/payments
# ═══════════════════════════════════════════════════════════════════

async def fetch_cashfree_payments(cf_order_id: str) -> list[dict] | None:
    """
    Independent server-side check of an order's payment attempts,
    straight from Cashfree — used by the admin reconcile endpoint
    (and could be polled from elsewhere) when we can't rely on a
    webhook having arrived. Returns None on any request failure.
    """
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(
                f"{CASHFREE_BASE_URL}/orders/{cf_order_id}/payments",
                headers=_cf_headers(),
            )
    except Exception as e:
        logger.error(f"Cashfree fetch-payments request failed for {cf_order_id}: {e}", exc_info=True)
        return None

    if resp.status_code != 200:
        logger.error(f"Cashfree fetch-payments failed for {cf_order_id}: {resp.status_code} {resp.text[:500]}")
        return None
    return resp.json()


# ═══════════════════════════════════════════════════════════════════
# Shared processing — used by both the live webhook and /reconcile
# ═══════════════════════════════════════════════════════════════════

async def _mark_group_orders_paid(checkout_group_id: str):
    try:
        await run_query(
            supabase_admin.table("orders")
            .update({"payment_status": "verified", "status": "confirmed"})
            .eq("checkout_group_id", checkout_group_id)
            .neq("payment_status", "verified")
        )
        await cache_delete("orders:recent")
    except Exception as e:
        logger.error(f"Failed to mark orders paid for group {checkout_group_id}: {e}", exc_info=True)


async def _mark_group_orders_payment_failed(checkout_group_id: str):
    try:
        await run_query(
            supabase_admin.table("orders")
            .update({"payment_status": "failed"})
            .eq("checkout_group_id", checkout_group_id)
            .eq("payment_status", "awaiting_payment")  # never downgrade a verified/already-final order
        )
    except Exception as e:
        logger.error(f"Failed to mark orders payment-failed for group {checkout_group_id}: {e}", exc_info=True)


async def process_cashfree_payment_event(payload: dict) -> str:
    """
    Applies one Cashfree payment event (from a verified webhook, or a
    reconcile-endpoint fetch reshaped into the same event shape) to
    payment_records + orders. Never raises for "nothing to do" cases
    (unknown order, duplicate/no-op status) — returns a short reason
    string instead so the caller can log/respond appropriately.

    This function assumes the caller has ALREADY verified authenticity
    (webhook signature, or the fact that the data came from our own
    authenticated Cashfree API call) — it does no auth of its own.
    """
    data = payload.get("data", {}) or {}
    order_block = data.get("order", {}) or {}
    payment_block = data.get("payment", {}) or {}

    cf_order_id = order_block.get("order_id")
    if not cf_order_id:
        logger.error(f"Cashfree event missing order_id: {payload}")
        return "missing_order_id"

    cf_payment_id = payment_block.get("cf_payment_id")
    cf_payment_status = payment_block.get("payment_status")
    payment_amount = payment_block.get("payment_amount")
    payment_method_obj = payment_block.get("payment_method") or {}
    payment_method = next(iter(payment_method_obj.keys()), None) if isinstance(payment_method_obj, dict) else None

    mapped_status = _map_cf_status(cf_payment_status)

    res = await run_query(
        supabase_admin.table("payment_records").select("*").eq("cashfree_order_id", cf_order_id).maybe_single()
    )
    record = res.data
    if not record:
        # We always create this row ourselves in create-session before
        # returning a payment_session_id to the browser, so this means
        # the event doesn't correspond to an order we created — ignore.
        logger.error(f"Cashfree event for unknown cashfree_order_id={cf_order_id} — no payment_records row. Ignoring.")
        return "unknown_order"

    now = datetime.now(timezone.utc).isoformat()

    if record["payment_status"] == mapped_status:
        # Duplicate delivery / no state change — refresh the audit trail
        # but skip re-running the orders-table side effect.
        try:
            await run_query(
                supabase_admin.table("payment_records")
                .update({"gateway_response": payload, "webhook_received_at": now})
                .eq("id", record["id"])
            )
        except Exception as e:
            logger.warning(f"Failed to refresh audit trail for {cf_order_id}: {e}")
        return "no_change"

    if payment_amount is not None:
        try:
            if abs(float(payment_amount) - float(record["amount"])) > 0.01:
                logger.warning(
                    f"Cashfree amount mismatch for {cf_order_id}: "
                    f"expected {record['amount']}, webhook says {payment_amount}"
                )
        except (TypeError, ValueError):
            pass

    # Conditional update keyed on the status we last saw: if two events
    # for this order race each other, only one of them actually flips
    # the row (and therefore runs the side effect below).
    update_res = await run_query(
        supabase_admin.table("payment_records")
        .update({
            "payment_status": mapped_status,
            "cashfree_payment_id": cf_payment_id,
            "payment_method": payment_method,
            "gateway_response": payload,
            "webhook_received_at": now,
            "updated_at": now,
        })
        .eq("id", record["id"])
        .eq("payment_status", record["payment_status"])
    )
    if not update_res.data:
        logger.info(f"payment_records for {cf_order_id} changed concurrently — skipping duplicate processing")
        return "race_skipped"

    logger.info(f"Cashfree payment_records updated: cf_order_id={cf_order_id} -> {mapped_status}")

    if mapped_status == "SUCCESS":
        await _mark_group_orders_paid(record["checkout_group_id"])
    elif mapped_status in ("FAILED", "CANCELLED", "USER_DROPPED"):
        await _mark_group_orders_payment_failed(record["checkout_group_id"])
    # PENDING: nothing further to do on the orders table.

    return mapped_status


# ═══════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════

class CreateSessionRequest(BaseModel):
    checkout_group_id: str = Field(..., min_length=1, max_length=64)


@router.post("/create-session")
@limiter.limit("10/minute")
async def create_payment_session(req: CreateSessionRequest, request: Request, customer=Depends(require_customer)):
    """
    Creates a Cashfree order for every order row in `checkout_group_id`
    that belongs to the logged-in customer, and returns the
    payment_session_id the frontend passes to Cashfree's Checkout JS SDK.
    """
    if not _cashfree_configured():
        raise HTTPException(
            status_code=503,
            detail="Online payment is not available right now. Please choose Cash on Delivery."
        )

    try:
        res = await run_query(
            supabase_admin.table("orders").select("*")
            .eq("checkout_group_id", req.checkout_group_id)
            .eq("user_id", customer["sub"])
        )
    except Exception as e:
        logger.error(f"Failed to fetch orders for checkout_group_id={req.checkout_group_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start payment. Please try again.")

    orders = res.data or []
    if not orders:
        raise HTTPException(status_code=404, detail="Checkout group not found")

    if any(o.get("payment_status") == "verified" for o in orders):
        raise HTTPException(status_code=409, detail="This order has already been paid for")

    total_amount = round(
        sum(float(o.get("our_price") or 0) + float(o.get("delivery_fee") or 0) for o in orders), 2
    )
    if total_amount <= 0:
        raise HTTPException(status_code=400, detail="Invalid order amount")

    cf_order_id = f"clv_{uuid.uuid4().hex}"
    first = orders[0]

    customer_details = {
        # Cashfree requires customer_id to be alphanumeric only — strip
        # the UUID's hyphens.
        "customer_id": str(customer["sub"]).replace("-", ""),
        "customer_phone": first.get("customer_phone", ""),
    }
    if first.get("customer_name"):
        customer_details["customer_name"] = first["customer_name"]
    if customer.get("email"):
        customer_details["customer_email"] = customer["email"]

    order_tags = {
        "checkout_group_id": str(req.checkout_group_id)[:255],
        "source": "clovical_web",
    }

    cf_response = await create_cashfree_order(cf_order_id, total_amount, customer_details, order_tags)
    payment_session_id = cf_response["payment_session_id"]

    try:
        await run_query(
            supabase_admin.table("payment_records").insert({
                "checkout_group_id": req.checkout_group_id,
                "customer_id": customer["sub"],
                "cashfree_order_id": cf_order_id,
                "amount": total_amount,
                "currency": "INR",
                "payment_status": "PENDING",
            })
        )
        await run_query(
            supabase_admin.table("orders")
            .update({"payment_type": "cashfree", "payment_status": "awaiting_payment"})
            .eq("checkout_group_id", req.checkout_group_id)
        )
    except Exception as e:
        # The Cashfree order already exists at this point but we couldn't
        # record it — log loudly so it's not silently orphaned; the
        # unique constraint on cashfree_order_id means a retry with the
        # same order_id would fail, but a fresh create-session call
        # (new cf_order_id) will still work fine for the customer.
        logger.error(f"Failed to persist payment_records for cf_order_id={cf_order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to start payment. Please try again.")

    logger.info(f"Cashfree session created: cf_order_id={cf_order_id} group={req.checkout_group_id} amount={total_amount}")

    return {
        "success": True,
        "payment_session_id": payment_session_id,
        "cashfree_order_id": cf_order_id,
        "amount": total_amount,
        "cashfree_env": CASHFREE_ENV,
    }


@router.post("/webhook")
async def cashfree_webhook(request: Request):
    """
    Cashfree calls this — never the browser. Signature is verified
    against the RAW body before any JSON parsing (parsing first can
    reformat numbers and break the signature check).
    """
    raw_body = await request.body()
    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")

    if not verify_cashfree_webhook_signature(raw_body, timestamp, signature):
        logger.warning("Cashfree webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        logger.error(f"Cashfree webhook: invalid JSON body: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")

    logger.info(f"Cashfree webhook received: type={payload.get('type')} event_time={payload.get('event_time')}")

    try:
        result = await process_cashfree_payment_event(payload)
    except Exception as e:
        # Returning 5xx makes Cashfree retry — correct behaviour for a
        # bug/transient failure on our side (unlike "unknown_order",
        # which is handled inside process_cashfree_payment_event and
        # returns normally, since retrying that wouldn't help).
        logger.error(f"Cashfree webhook processing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Webhook processing failed")

    return {"status": "ok", "result": result}


@router.get("/status/{checkout_group_id}")
async def payment_status(checkout_group_id: str, customer=Depends(require_customer)):
    """
    Read-only status lookup for the frontend to poll after the Cashfree
    redirect. Reflects only what the webhook has already written to the
    DB — this endpoint never marks anything as paid.
    """
    try:
        res = await run_query(
            supabase_admin.table("payment_records").select("*")
            .eq("checkout_group_id", checkout_group_id)
            .eq("customer_id", customer["sub"])
            .order("created_at", desc=True)
            .limit(1)
            .maybe_single()
        )
    except Exception as e:
        logger.error(f"Failed to fetch payment status for group {checkout_group_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch payment status")

    record = res.data
    if not record:
        raise HTTPException(status_code=404, detail="No payment attempt found for this checkout")

    return {
        "checkout_group_id": checkout_group_id,
        "payment_status": record["payment_status"],
        "amount": record["amount"],
        "cashfree_order_id": record["cashfree_order_id"],
        "updated_at": record.get("updated_at"),
    }


@router.post("/reconcile/{cashfree_order_id}")
async def reconcile_payment(cashfree_order_id: str, admin=Depends(require_admin)):
    """
    Admin fallback: re-fetches payment status directly from Cashfree
    (independent of any webhook) and reconciles our DB via the same
    processing path the live webhook uses. For the rare case a webhook
    delivery never arrives despite Cashfree's own retries.
    """
    if not _cashfree_configured():
        raise HTTPException(status_code=503, detail="Cashfree is not configured")

    payments = await fetch_cashfree_payments(cashfree_order_id)
    if payments is None:
        raise HTTPException(status_code=502, detail="Could not fetch payment status from Cashfree")

    if not payments:
        return {"success": True, "message": "No payment attempts recorded on Cashfree for this order yet"}

    # Prefer a SUCCESS attempt if one exists; otherwise reconcile against
    # the most recent attempt.
    chosen = next((p for p in payments if p.get("payment_status") == "SUCCESS"), payments[-1])

    synthetic_event = {
        "data": {
            "order": {"order_id": cashfree_order_id},
            "payment": chosen,
        },
        "type": f"PAYMENT_{chosen.get('payment_status', 'PENDING')}_WEBHOOK_RECONCILED",
        "event_time": datetime.now(timezone.utc).isoformat(),
    }
    result = await process_cashfree_payment_event(synthetic_event)

    return {"success": True, "reconciled_status": chosen.get("payment_status"), "result": result, "payments": payments}