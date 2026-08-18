import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from utils.db import supabase_admin, run_query
from utils.auth_utils import verify_password, create_shopkeeper_token
from utils.login_throttle import is_blocked, record_failure, clear as clear_throttle

logger = logging.getLogger(__name__)
router = APIRouter()


class ShopkeeperLoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: ShopkeeperLoginRequest, request: Request):
    client_ip = request.headers.get("CF-Connecting-IP") or request.client.host

    # Reuses the same IP block-list as admin login (utils/login_throttle.py)
    # — one shared brute-force guard per IP across both login surfaces.
    if await is_blocked(client_ip):
        logger.warning(f"Shopkeeper login blocked IP: {client_ip}")
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try later.")

    try:
        res = await run_query(
            supabase_admin.table("shopkeepers").select("id,username,password,shop_name").eq("username", req.username).single()
        )
    except Exception:
        res = None

    if not res or not res.data or not res.data.get("password"):
        await record_failure(client_ip, "Shopkeeper")
        logger.warning(f"Shopkeeper login failed: username={req.username}, IP={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    sk = res.data
    if not verify_password(req.password, sk["password"]):
        await record_failure(client_ip, "Shopkeeper")
        logger.warning(f"Shopkeeper login failed: username={req.username}, IP={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    await clear_throttle(client_ip)
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