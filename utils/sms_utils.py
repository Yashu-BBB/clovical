"""
utils/sms_utils.py — MSG91 OTP sending utility
================================================
Uses MSG91's OTP API v5 to send a 6-digit OTP to an Indian mobile number.
All credentials are read from environment variables — nothing is hard-coded.

MSG91 API reference: https://docs.msg91.com/reference/send-otp
"""
import os
import json
import logging
import httpx

logger = logging.getLogger(__name__)

MSG91_AUTH_KEY   = os.getenv("MSG91_AUTH_KEY", "")
MSG91_TEMPLATE_ID = os.getenv("MSG91_TEMPLATE_ID", "")
MSG91_SENDER_ID  = os.getenv("MSG91_SENDER_ID", "CLOVICL")

_MSG91_SEND_URL   = "https://control.msg91.com/api/v5/otp"
_MSG91_VERIFY_URL = "https://control.msg91.com/api/v5/otp/verify"

# For development/sandbox, set SMS_MOCK=true in env to skip actual sends
_SMS_MOCK = os.getenv("SMS_MOCK", "false").lower() == "true"


async def send_otp(phone: str, otp: str) -> bool:
    """
    Send a 6-digit OTP to the given 10-digit Indian mobile number via MSG91.

    phone  — bare 10-digit number (no +91 prefix)
    otp    — the OTP string to embed in the template

    Returns True on success, False on failure (never raises — a send failure
    must never block the caller; the caller should surface a user-friendly error).
    """
    if not MSG91_AUTH_KEY or not MSG91_TEMPLATE_ID:
        logger.warning("MSG91 credentials not configured — OTP send skipped (set MSG91_AUTH_KEY and MSG91_TEMPLATE_ID)")
        return False

    if _SMS_MOCK:
        logger.info(f"[SMS_MOCK] Would send OTP {otp} to {phone}")
        return True

    mobile = f"91{phone}"   # MSG91 expects country-code prefix
    payload = {
        "template_id": MSG91_TEMPLATE_ID,
        "mobile":      mobile,
        "authkey":     MSG91_AUTH_KEY,
        "otp":         otp,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _MSG91_SEND_URL,
                headers={
                    "Content-Type": "application/json",
                    "authkey": MSG91_AUTH_KEY,
                },
                content=json.dumps(payload),
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("type") == "success":
                logger.info(f"OTP sent to {phone} via MSG91")
                return True
            else:
                logger.error(f"MSG91 OTP send failed for {phone}: status={resp.status_code} body={data}")
                return False
    except Exception as e:
        logger.error(f"MSG91 OTP send exception for {phone}: {e}", exc_info=True)
        return False


def is_configured() -> bool:
    """True when MSG91 credentials are present in environment."""
    return bool(MSG91_AUTH_KEY and MSG91_TEMPLATE_ID)
