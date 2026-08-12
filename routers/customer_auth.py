"""
routers/customer_auth.py — Customer authentication
====================================================
Provides two login methods for shoppers at checkout:

  1. Google OAuth (server-side Authorization Code flow)
       GET  /api/customer-auth/google           → redirect to Google
       GET  /api/customer-auth/google/callback  → exchange code, issue cookie

  2. Mobile phone OTP (MSG91)
       POST /api/customer-auth/otp/send         → verify Turnstile, send OTP
       POST /api/customer-auth/otp/verify       → check OTP, issue cookie

  3. Session management
       GET  /api/customer-auth/me               → current session info
       POST /api/customer-auth/logout           → clear cookie

Design notes
────────────
• Uses the same httponly + secure + samesite=lax cookie pattern as the existing
  admin and shopkeeper sessions in utils/auth_utils.py. Cookie name is
  "customer_token" (distinct from admin_token / shopkeeper_token).
• Google state parameter stored in Redis (10-min TTL) for CSRF protection.
• OTP stored in Redis: key "otp:{phone}", value JSON {otp, attempts, sent_count}.
  TTL = 5 minutes. Max 3 wrong attempts → invalidate. Max 3 sends per 10 minutes.
• Duplicate-account prevention:
    - Google login  → upsert on google_id. If same email already in DB from an
      OTP login, merge google_id + auth_provider onto the existing record.
    - OTP login     → upsert on phone. If same phone already in DB from a Google
      login, merge phone + auth_provider onto the existing record.
• Never raises on OTP send failure — returns a user-friendly error instead.
"""
import os
import re
import json
import logging
import secrets
import random
import string
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import create_customer_token, get_customer_from_request
from utils.captcha import verify_turnstile
from utils.sms_utils import send_otp as sms_send_otp
from utils.cache import redis_client

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Environment ────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI  = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://www.clovical.in/api/customer-auth/google/callback"
)
BASE_URL = os.getenv("BASE_URL", "https://www.clovical.in")

_OTP_TTL          = 300     # 5 minutes
_OTP_MAX_ATTEMPTS = 3       # wrong guesses before OTP is invalidated
_OTP_RATE_WINDOW  = 600     # 10-minute window for send rate limiting
_OTP_RATE_LIMIT   = 3       # max OTPs sent per phone per window
_STATE_TTL        = 600     # 10-minute Google OAuth state validity


# ═══════════════════════════════════════════════════════════════════
# Helper: upsert or merge customer record
# ═══════════════════════════════════════════════════════════════════

async def _upsert_customer_google(google_id: str, email: str, name: str) -> dict:
    """
    Find or create a customer record for a Google login.

    Merge logic:
    1. Look up by google_id → found → update name/email if changed, return.
    2. Look up by email (existing OTP customer) → merge google_id onto record.
    3. Neither → create new record.
    """
    # 1. Exact match on google_id
    res = await run_query(
        supabase_admin.table("customers")
        .select("*")
        .eq("google_id", google_id)
        .maybe_single()
    )
    if res.data:
        customer = res.data
        # Refresh name/email in case they changed in Google profile
        updates = {}
        if name and customer.get("name") != name:
            updates["name"] = name
        if email and customer.get("email") != email:
            updates["email"] = email
        if updates:
            updates["updated_at"] = datetime.now(timezone.utc).isoformat()
            await run_query(
                supabase_admin.table("customers").update(updates).eq("id", customer["id"])
            )
        return {**customer, **updates}

    # 2. Same email exists (OTP user) → merge
    if email:
        res2 = await run_query(
            supabase_admin.table("customers")
            .select("*")
            .eq("email", email)
            .maybe_single()
        )
        if res2.data:
            customer = res2.data
            merge = {
                "google_id": google_id,
                "auth_provider": "both" if customer.get("phone") else "google",
                "name": name or customer.get("name"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            await run_query(
                supabase_admin.table("customers").update(merge).eq("id", customer["id"])
            )
            return {**customer, **merge}

    # 3. Create new
    new = {
        "google_id": google_id,
        "email": email,
        "name": name,
        "auth_provider": "google",
    }
    res3 = await run_query(supabase_admin.table("customers").insert(new))
    return res3.data[0]


async def _upsert_customer_phone(phone: str) -> dict:
    """
    Find or create a customer record for an OTP-verified mobile number.

    If a Google-authenticated customer already has this phone number (rare,
    since we don't collect phone from Google), merge it. Otherwise upsert on phone.
    """
    res = await run_query(
        supabase_admin.table("customers")
        .select("*")
        .eq("phone", phone)
        .maybe_single()
    )
    if res.data:
        customer = res.data
        # Ensure auth_provider reflects phone login
        if customer.get("auth_provider") == "google":
            await run_query(
                supabase_admin.table("customers")
                .update({"auth_provider": "both", "updated_at": datetime.now(timezone.utc).isoformat()})
                .eq("id", customer["id"])
            )
            customer["auth_provider"] = "both"
        return customer

    new = {"phone": phone, "auth_provider": "phone"}
    res2 = await run_query(supabase_admin.table("customers").insert(new))
    return res2.data[0]


def _set_customer_cookie(response: JSONResponse | RedirectResponse, token: str):
    response.set_cookie(
        "customer_token",
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=3600 * 24 * 7,  # 7 days
        path="/",
    )


# ═══════════════════════════════════════════════════════════════════
# Google OAuth — server-side Authorization Code flow
# ═══════════════════════════════════════════════════════════════════

def _google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _build_google_auth_url(state: str) -> str:
    """Build the Google OAuth authorization URL manually (no library dependency)."""
    import urllib.parse
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


async def _exchange_google_code(code: str) -> dict | None:
    """Exchange an authorization code for an ID token via Google's token endpoint."""
    import urllib.parse
    try:
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code":          code,
                    "client_id":     GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri":  GOOGLE_REDIRECT_URI,
                    "grant_type":    "authorization_code",
                },
            )
            if resp.status_code != 200:
                logger.error(f"Google token exchange failed: {resp.text}")
                return None
            return resp.json()
    except Exception as e:
        logger.error(f"Google token exchange exception: {e}", exc_info=True)
        return None


async def _verify_google_id_token(id_token: str) -> dict | None:
    """
    Verify a Google ID token using Google's tokeninfo endpoint.
    Returns the decoded payload or None on failure.
    """
    try:
        async with __import__("httpx").AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": id_token},
            )
            if resp.status_code != 200:
                logger.warning(f"Google tokeninfo rejected: {resp.text}")
                return None
            data = resp.json()
            # Verify the token was issued for our app
            if data.get("aud") != GOOGLE_CLIENT_ID:
                logger.warning(f"Google token audience mismatch: {data.get('aud')}")
                return None
            return data
    except Exception as e:
        logger.error(f"Google ID token verification exception: {e}", exc_info=True)
        return None


@router.get("/google")
async def google_login(request: Request, next: str = "/checkout"):
    """Redirect the customer's browser to Google's OAuth consent screen."""
    if not _google_configured():
        raise HTTPException(
            status_code=503,
            detail="Google Sign-In is not configured on this server."
        )

    # Generate a CSRF state token and store it in Redis for 10 minutes
    state = secrets.token_urlsafe(32)
    if redis_client:
        await redis_client.setex(
            f"google_state:{state}",
            _STATE_TTL,
            json.dumps({"next": next})
        )

    auth_url = _build_google_auth_url(state)
    return RedirectResponse(auth_url)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Receive Google's callback, verify state + code, issue customer_token cookie."""
    if error:
        logger.warning(f"Google OAuth error: {error}")
        return RedirectResponse("/checkout?login_error=google_cancelled")

    if not code or not state:
        return RedirectResponse("/checkout?login_error=invalid_callback")

    # CSRF: verify state exists in Redis
    next_url = "/checkout"
    if redis_client:
        stored = await redis_client.get(f"google_state:{state}")
        if not stored:
            logger.warning("Google OAuth: state not found in Redis (CSRF or expired)")
            return RedirectResponse("/checkout?login_error=state_invalid")
        try:
            next_url = json.loads(stored).get("next", "/checkout")
        except Exception:
            pass
        # Open-redirect guard: next_url round-trips through Redis un-
        # validated from the original ?next= query param, so restrict it
        # to a same-origin relative path before ever using it in a
        # redirect (a bare "/x" is fine; "//evil.com" or "https://evil.com"
        # is not — browsers treat "//" as protocol-relative).
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = "/checkout"
        await redis_client.delete(f"google_state:{state}")
    else:
        logger.warning("Redis unavailable — Google OAuth state check skipped (non-production safe)")

    # Exchange code for tokens
    token_data = await _exchange_google_code(code)
    if not token_data or "id_token" not in token_data:
        return RedirectResponse("/checkout?login_error=token_exchange_failed")

    # Verify the ID token
    id_info = await _verify_google_id_token(token_data["id_token"])
    if not id_info:
        return RedirectResponse("/checkout?login_error=token_verification_failed")

    google_id = id_info.get("sub")
    email     = id_info.get("email", "")
    name      = id_info.get("name", "")

    if not google_id:
        return RedirectResponse("/checkout?login_error=no_google_id")

    try:
        customer = await _upsert_customer_google(google_id, email, name)
    except Exception as e:
        logger.error(f"Google login: customer upsert failed: {e}", exc_info=True)
        return RedirectResponse("/checkout?login_error=db_error")

    token    = create_customer_token(customer["id"], customer.get("phone"), customer.get("email"))
    # Pre-existing bug fix: next_url was computed above from the stored
    # OAuth state but never actually used — this always redirected to
    # /checkout regardless of where the sign-in was initiated from. Needed
    # so Google sign-in from /my-orders (next=%2Fmy-orders) lands back on
    # /my-orders instead of /checkout. Error-path redirects above are left
    # as /checkout unchanged — out of scope here.
    response = RedirectResponse(f"{next_url}?login=success")
    _set_customer_cookie(response, token)
    logger.info(f"Customer Google login: id={customer['id']} email={email}")
    return response


# ═══════════════════════════════════════════════════════════════════
# Phone / OTP
# ═══════════════════════════════════════════════════════════════════

class OTPSendRequest(BaseModel):
    phone:         str = Field(..., min_length=10, max_length=10)
    captcha_token: str = Field("", max_length=2000)


class OTPVerifyRequest(BaseModel):
    phone: str = Field(..., min_length=10, max_length=10)
    otp:   str = Field(..., min_length=4, max_length=8)


def _validate_phone(phone: str) -> str:
    """Strip spaces/dashes, validate 10-digit Indian mobile. Returns cleaned phone."""
    cleaned = phone.strip().replace(" ", "").replace("-", "")
    if not re.match(r"^[6-9]\d{9}$", cleaned):
        raise HTTPException(status_code=400, detail="Invalid Indian mobile number")
    return cleaned


def _generate_otp(length: int = 6) -> str:
    return "".join(random.choices(string.digits, k=length))


@router.post("/otp/send")
async def send_otp(req: OTPSendRequest, request: Request):
    """
    Verify Turnstile, enforce rate limits, generate and send a 6-digit OTP.
    Rate limit: max 3 OTPs per phone per 10 minutes (tracked in Redis).
    """
    client_ip = request.headers.get("CF-Connecting-IP") or request.client.host
    phone     = _validate_phone(req.phone)

    # Turnstile CAPTCHA verification (protects this endpoint from bots)
    if not req.captcha_token:
        raise HTTPException(status_code=400, detail="Human verification required")
    if not verify_turnstile(req.captcha_token, client_ip):
        raise HTTPException(status_code=400, detail="Human verification failed. Please try again.")

    # Rate limiting: track how many OTPs have been sent to this phone
    rate_key = f"otp_rate:{phone}"
    if redis_client:
        sent_count_raw = await redis_client.get(rate_key)
        sent_count = int(sent_count_raw) if sent_count_raw else 0
        if sent_count >= _OTP_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many OTP requests. Please wait 10 minutes before trying again."
            )

    # Generate OTP and store in Redis
    otp = _generate_otp()
    otp_key = f"otp:{phone}"
    otp_data = json.dumps({"otp": otp, "attempts": 0})

    if redis_client:
        await redis_client.setex(otp_key, _OTP_TTL, otp_data)
        # Increment rate counter (set with window TTL if first in window)
        pipe = redis_client.pipeline()
        pipe.incr(rate_key)
        pipe.expire(rate_key, _OTP_RATE_WINDOW, nx=True)
        await pipe.execute()
    else:
        logger.warning("Redis unavailable — OTP rate limiting and storage skipped")

    # Send OTP via MSG91
    sent = await sms_send_otp(phone, otp)
    if not sent:
        raise HTTPException(
            status_code=503,
            detail="Could not send OTP. Please try again or use Google Sign-In."
        )

    logger.info(f"OTP sent to phone ending ...{phone[-4:]}")
    return {"success": True, "message": "OTP sent successfully"}


@router.post("/otp/verify")
async def verify_otp(req: OTPVerifyRequest):
    """
    Verify the OTP from Redis. On success, upsert the customer and issue a session cookie.
    Max 3 wrong attempts → OTP is invalidated (must request a new one).
    """
    phone = _validate_phone(req.phone)
    otp   = req.otp.strip()

    if not redis_client:
        raise HTTPException(
            status_code=503,
            detail="Session service unavailable. Please try again."
        )

    otp_key = f"otp:{phone}"
    stored_raw = await redis_client.get(otp_key)

    if not stored_raw:
        raise HTTPException(
            status_code=400,
            detail="OTP expired or not found. Please request a new OTP."
        )

    try:
        stored = json.loads(stored_raw)
    except Exception:
        await redis_client.delete(otp_key)
        raise HTTPException(status_code=400, detail="Invalid OTP state. Please request a new OTP.")

    attempts = stored.get("attempts", 0)
    if attempts >= _OTP_MAX_ATTEMPTS:
        await redis_client.delete(otp_key)
        raise HTTPException(
            status_code=400,
            detail="Too many incorrect attempts. Please request a new OTP."
        )

    if stored.get("otp") != otp:
        # Increment attempt counter
        stored["attempts"] = attempts + 1
        await redis_client.setex(otp_key, _OTP_TTL, json.dumps(stored))
        remaining = _OTP_MAX_ATTEMPTS - stored["attempts"]
        raise HTTPException(
            status_code=400,
            detail=f"Incorrect OTP. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
        )

    # ✅ OTP correct — delete it immediately (single-use)
    await redis_client.delete(otp_key)

    try:
        customer = await _upsert_customer_phone(phone)
    except Exception as e:
        logger.error(f"OTP verify: customer upsert failed for phone ...{phone[-4:]}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Login failed. Please try again.")

    token = create_customer_token(customer["id"], customer.get("phone"), customer.get("email"))
    response = JSONResponse({
        "success": True,
        "customer": {
            "id":    customer["id"],
            "phone": customer.get("phone"),
            "name":  customer.get("name"),
        }
    })
    _set_customer_cookie(response, token)
    logger.info(f"Customer OTP login: id={customer['id']} phone=...{phone[-4:]}")
    return response


# ═══════════════════════════════════════════════════════════════════
# Session management
# ═══════════════════════════════════════════════════════════════════

@router.get("/me")
async def get_me(request: Request):
    """Return the currently logged-in customer's info (or null if not logged in)."""
    customer = get_customer_from_request(request)
    if not customer:
        return {"logged_in": False, "customer": None}
    return {
        "logged_in": True,
        "customer": {
            "id":    customer["sub"],
            "phone": customer.get("phone") or None,
            "email": customer.get("email") or None,
        }
    }


@router.post("/logout")
async def logout():
    """Clear the customer_token cookie."""
    response = JSONResponse({"success": True})
    response.delete_cookie("customer_token", path="/")
    return response