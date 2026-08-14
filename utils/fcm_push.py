"""
utils/fcm_push.py — Firebase Cloud Messaging push dispatch
=============================================================
Standalone, best-effort push sender. Deliberately separate from
utils/notifications.py (the in-app notification writer) so the two stay
decoupled: this module never touches the `notifications` table, and
notifications.py never speaks HTTP to Google.

NEVER raises — every public function here swallows and logs its own
errors, exactly like the rest of utils/notifications.py, because a push
failing to send must never break the order/request/stock operation that
triggered it.

Auth model
----------
Uses a Firebase service-account (NOT the old legacy server key, which
Google is retiring) to mint short-lived OAuth2 access tokens for the
FCM HTTP v1 API: https://fcm.googleapis.com/v1/projects/{project}/messages:send

Required environment variables (see README section added by this change
for the full Firebase-console walkthrough):
  FIREBASE_PROJECT_ID              — Firebase project ID (e.g. "clovical-12345")
  FIREBASE_SERVICE_ACCOUNT_JSON_B64 — the service account JSON key file,
                                       base64-encoded (preferred — avoids
                                       Railway mangling newlines in the
                                       private_key field)
  FIREBASE_SERVICE_ACCOUNT_JSON     — same JSON, raw/unencoded (fallback,
                                       only used if the _B64 var is unset)

If none of these are set, every function here is a silent no-op — the
in-app notification system keeps working exactly as before.
"""
import os
import json
import base64
import logging
import time

import httpx
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

from utils.db import supabase_admin, run_query, run_blocking

logger = logging.getLogger(__name__)

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
_RAW_SA_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "")
_B64_SA_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_B64", "")

_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_SEND_URL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"
_REQUEST_TIMEOUT = 10

_credentials = None  # lazily built google.oauth2.service_account.Credentials
_init_failed = False  # sticky — don't retry parsing a broken key on every send


def _load_service_account_info() -> dict | None:
    raw = None
    if _B64_SA_JSON:
        try:
            raw = base64.b64decode(_B64_SA_JSON).decode("utf-8")
        except Exception as e:
            logger.error(f"FIREBASE_SERVICE_ACCOUNT_JSON_B64 is not valid base64: {e}")
            return None
    elif _RAW_SA_JSON:
        raw = _RAW_SA_JSON
    else:
        return None

    try:
        return json.loads(raw)
    except Exception as e:
        logger.error(f"Firebase service account JSON could not be parsed: {e}")
        return None


def is_configured() -> bool:
    """True if enough env vars are present to attempt FCM sends. Mirrors
    utils/nimbuspost.py's is_configured() convention."""
    if not FIREBASE_PROJECT_ID or not (_B64_SA_JSON or _RAW_SA_JSON):
        return False
    return True


def _get_credentials():
    global _credentials, _init_failed
    if _credentials is not None:
        return _credentials
    if _init_failed:
        return None
    info = _load_service_account_info()
    if not info:
        _init_failed = True
        return None
    try:
        _credentials = service_account.Credentials.from_service_account_info(info, scopes=[_FCM_SCOPE])
        return _credentials
    except Exception as e:
        logger.error(f"Failed to build Firebase credentials from service account: {e}", exc_info=True)
        _init_failed = True
        return None


def _get_access_token_sync() -> str | None:
    """Blocking — always call via run_blocking(). Refreshes the cached
    credentials object only when its token is missing/expired, so most
    calls are just an in-memory check, not a network round trip."""
    creds = _get_credentials()
    if not creds:
        return None
    try:
        if not creds.valid:
            creds.refresh(GoogleAuthRequest())
        return creds.token
    except Exception as e:
        logger.error(f"Failed to refresh Firebase access token: {e}", exc_info=True)
        return None


def _send_one_sync(token: str, title: str, body: str, link: str | None) -> str:
    """
    Blocking — always call via run_blocking(). Sends one FCM message and
    returns one of: "ok", "invalid_token", "error".
    "invalid_token" means FCM confirmed the token is dead (unregistered,
    app uninstalled, etc.) and the caller should delete it from the DB.
    """
    access_token = _get_access_token_sync()
    if not access_token:
        return "error"

    payload = {
        "message": {
            "token": token,
            "notification": {"title": title, "body": body or ""},
            "webpush": {
                "notification": {"icon": "/static/images/favicon.svg"},
                "fcm_options": {"link": link} if link else {},
            },
            "data": {k: v for k, v in {"link": link}.items() if v is not None},
        }
    }
    url = _FCM_SEND_URL.format(project_id=FIREBASE_PROJECT_ID)
    try:
        resp = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; UTF-8",
            },
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )
    except Exception as e:
        logger.warning(f"FCM send request failed: {e}")
        return "error"

    if resp.status_code == 200:
        return "ok"

    # FCM v1 error shape: {"error": {"status": "NOT_FOUND"/"INVALID_ARGUMENT", ...}}
    try:
        err_status = (resp.json().get("error") or {}).get("status", "")
    except Exception:
        err_status = ""
    if resp.status_code in (404, 400) and err_status in ("NOT_FOUND", "INVALID_ARGUMENT", "UNREGISTERED"):
        return "invalid_token"

    logger.warning(f"FCM send failed ({resp.status_code}): {resp.text[:300]}")
    return "error"


async def _tokens_for(recipient_type: str, recipient_id) -> list[str]:
    q = supabase_admin.table("push_subscriptions").select("fcm_token").eq("recipient_type", recipient_type)
    q = q.is_("recipient_id", "null") if recipient_type == "admin" else q.eq("recipient_id", str(recipient_id))
    res = await run_query(q)
    return [row["fcm_token"] for row in (res.data or []) if row.get("fcm_token")]


async def _delete_token(token: str):
    try:
        await run_query(supabase_admin.table("push_subscriptions").delete().eq("fcm_token", token))
    except Exception as e:
        logger.warning(f"Failed to delete dead FCM token: {e}")


async def send_push(recipient_type: str, recipient_id, title: str, body: str = "", link: str | None = None):
    """
    Sends a push to every device registered for this recipient. Fire this
    with asyncio.create_task (see utils/notifications.py) — it's async and
    safe to await directly too, but it never raises either way, and a slow
    FCM round trip should never hold up the caller.
    """
    if not is_configured():
        return
    try:
        tokens = await _tokens_for(recipient_type, recipient_id)
        if not tokens:
            return
        for token in tokens:
            result = await run_blocking(_send_one_sync, token, title, body, link)
            if result == "invalid_token":
                await _delete_token(token)
    except Exception as e:
        logger.error(f"send_push failed ({recipient_type}/{recipient_id}): {e}", exc_info=True)


async def register_token(recipient_type: str, recipient_id, fcm_token: str, user_agent: str | None = None):
    """
    Upserts a device's FCM token for this recipient. Keyed on the token
    itself (unique) so re-registering the same browser/device just
    refreshes ownership + timestamp, and a device that logs in as a
    different user automatically stops delivering to the previous owner.
    """
    row = {
        "recipient_type": recipient_type,
        "recipient_id": str(recipient_id) if recipient_id is not None else None,
        "fcm_token": fcm_token,
        "user_agent": (user_agent or "")[:300],
        "updated_at": "now()",
    }
    await run_query(
        supabase_admin.table("push_subscriptions").upsert(row, on_conflict="fcm_token")
    )