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
SHOPKEEPER_SESSION_TTL = 3600 * 4  # 4 hours, same as admin

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
    token = request.cookies.get("admin_token")
    if not token:
        return None
    return decode_token(token)

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