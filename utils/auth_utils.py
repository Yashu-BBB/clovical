import os
import time
import logging
import bcrypt
import jwt
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)

SECRET_KEY = os.getenv("SECRET_KEY", "changeme-in-production")
SESSION_TTL = 3600 * 4  # 4 hours
SHOPKEEPER_SESSION_TTL = 3600 * 4       # 4 hours
CUSTOMER_SESSION_TTL = 3600 * 24 * 7  # 7 days, same as admin

if SECRET_KEY == "changeme-in-production":
    logger.critical(
        "⚠️ SECURITY WARNING: SECRET_KEY is using default value! "
        "Set a strong SECRET_KEY in your environment variables immediately!"
    )

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "role": "admin",
        "iat": time.time(),
        "exp": time.time() + SESSION_TTL
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None

def get_admin_from_request(request: Request) -> dict | None:
    """SECURITY: every JWT in this app (admin/shopkeeper/customer) is signed
    with the same SECRET_KEY, so decode success alone does not prove a token
    was issued by the admin login — a valid shopkeeper_token or customer_token
    would decode just as successfully. The role check below is required, not
    optional; it's the same check get_shopkeeper_from_request/
    get_customer_from_request already do for their own cookies."""
    token = request.cookies.get("admin_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("role") != "admin":
        return None
    return payload

def require_admin(request: Request) -> dict:
    admin = get_admin_from_request(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return admin


# ─── Shopkeeper sessions ────────────────────────────────────────────────────
# Mirrors the admin token pattern above exactly, but on its own cookie
# ("shopkeeper_token") and JWT payload shape so a shopkeeper session can never
# be confused with — or reused as — an admin session. Both cookies can be
# present in the same browser at once without colliding.

def create_shopkeeper_token(shopkeeper_id: int, username: str) -> str:
    payload = {
        "sub": username,
        "shopkeeper_id": shopkeeper_id,
        "role": "shopkeeper",
        "iat": time.time(),
        "exp": time.time() + SHOPKEEPER_SESSION_TTL
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def get_shopkeeper_from_request(request: Request) -> dict | None:
    token = request.cookies.get("shopkeeper_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("role") != "shopkeeper":
        return None
    return payload

def require_shopkeeper(request: Request) -> dict:
    sk = get_shopkeeper_from_request(request)
    if not sk:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return sk


# ─── Customer sessions ──────────────────────────────────────────────────────
# Cookie: "customer_token" — httponly, secure, samesite=lax, 7-day TTL.
# Payload shape: {sub: customer_id, phone, email, role: "customer", iat, exp}
# Completely separate from admin/shopkeeper tokens — a customer cookie can
# never be mistaken for an admin/shopkeeper session (role claim differs, cookie
# name differs). Both admin and customer cookies can be present simultaneously
# without collision (e.g. an admin testing the storefront).

def create_customer_token(customer_id: str, phone: str | None, email: str | None) -> str:
    payload = {
        "sub": customer_id,
        "phone": phone or "",
        "email": email or "",
        "role": "customer",
        "iat": time.time(),
        "exp": time.time() + CUSTOMER_SESSION_TTL,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def get_customer_from_request(request: Request) -> dict | None:
    token = request.cookies.get("customer_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("role") != "customer":
        return None
    return payload


def require_customer(request: Request) -> dict:
    customer = get_customer_from_request(request)
    if not customer:
        raise HTTPException(status_code=401, detail="Login required")
    return customer