import time
import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.db import supabase_admin, run_query
from utils.auth_utils import verify_password, create_shopkeeper_token

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_ATTEMPTS = 3
BLOCK_DURATION = 900  # 15 minutes


class ShopkeeperLoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: ShopkeeperLoginRequest, request: Request):
    client_ip = request.headers.get("CF-Connecting-IP") or request.client.host
    app_state = request.app.state

    # Reuses the same IP block-list as admin login (app.state.blocked_ips) —
    # one shared brute-force guard per IP across both login surfaces.
    if client_ip in app_state.blocked_ips:
        if time.time() - app_state.blocked_ips[client_ip] < BLOCK_DURATION:
            logger.warning(f"Shopkeeper login blocked IP: {client_ip}")
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try later.")
        else:
            del app_state.blocked_ips[client_ip]
            app_state.failed_attempts.pop(client_ip, None)

    try:
        res = await run_query(
            supabase_admin.table("shopkeepers").select("id,username,password,shop_name").eq("username", req.username).single()
        )
    except Exception:
        res = None

    if not res or not res.data or not res.data.get("password"):
        app_state.failed_attempts[client_ip] = app_state.failed_attempts.get(client_ip, 0) + 1
        if app_state.failed_attempts[client_ip] >= MAX_ATTEMPTS:
            app_state.blocked_ips[client_ip] = time.time()
            logger.warning(f"Shopkeeper login: IP {client_ip} blocked after {MAX_ATTEMPTS} failed attempts")
        logger.warning(f"Shopkeeper login failed: username={req.username}, IP={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    sk = res.data
    if not verify_password(req.password, sk["password"]):
        app_state.failed_attempts[client_ip] = app_state.failed_attempts.get(client_ip, 0) + 1
        if app_state.failed_attempts[client_ip] >= MAX_ATTEMPTS:
            app_state.blocked_ips[client_ip] = time.time()
            logger.warning(f"Shopkeeper login: IP {client_ip} blocked")
        logger.warning(f"Shopkeeper login failed: username={req.username}, IP={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    app_state.failed_attempts.pop(client_ip, None)
    logger.info(f"Shopkeeper logged in: {req.username} (id={sk['id']})")
    token = create_shopkeeper_token(sk["id"], sk["username"])

    response = JSONResponse({"success": True, "shop_name": sk.get("shop_name")})
    response.set_cookie(
        "shopkeeper_token", token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=14400
    )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("shopkeeper_token")
    return response