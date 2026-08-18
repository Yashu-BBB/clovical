import logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from utils.db import supabase_admin, run_query
from utils.auth_utils import verify_password, create_token
from utils.login_throttle import is_blocked, record_failure, clear as clear_throttle

logger = logging.getLogger(__name__)
router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: LoginRequest, request: Request):
    client_ip = request.headers.get("CF-Connecting-IP") or request.client.host

    if await is_blocked(client_ip):
        logger.warning(f"Admin login blocked IP: {client_ip}")
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try later.")

    try:
        res = await run_query(supabase_admin.table("admins").select("*").eq("username", req.username).single())
    except Exception:
        res = None

    if not res or not res.data:
        await record_failure(client_ip, "Admin")
        logger.warning(f"Admin login failed: username={req.username}, IP={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    admin = res.data
    if not verify_password(req.password, admin["password"]):
        await record_failure(client_ip, "Admin")
        logger.warning(f"Admin login failed: username={req.username}, IP={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Clear failed attempts on success
    await clear_throttle(client_ip)
    logger.info(f"Admin logged in: {req.username}")
    token = create_token(req.username)

    response = JSONResponse({"success": True})
    response.set_cookie(
        "admin_token", token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=14400
    )
    return response


@router.post("/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie("admin_token")
    return response