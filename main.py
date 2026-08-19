from dotenv import load_dotenv
load_dotenv()  
import logging
import time
import hashlib
import asyncio
import sentry_sdk
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import redis.asyncio as aioredis
import os

from routers import auth, products, orders, shopkeepers, admin, analytics, public, categories, shopkeeper_auth, shopkeeper_panel, product_requests, customer_auth, payments, notifications, cart
from utils.db import supabase_admin, run_query, run_blocking
from utils.cache import init_redis, close_redis
from utils.login_throttle import is_blocked as login_ip_blocked

# ─── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-7s | %(filename)s:%(lineno)d | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ─── Sentry ────────────────────────────────────────────────────────────────
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1)

# ─── Rate Limiter ──────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

# ─── Visitor IP hash salt (prevents SHA256 rainbow-table lookups on ip_hash) ─
HASH_SALT = os.getenv("SECRET_KEY", "salt")[:16]

# ─── Blocked IPs ───────────────────────────────────────────────────────────
# Moved to utils/login_throttle.py (Redis-backed, in-memory fallback) so the
# block-list survives restarts and is shared across instances — see that
# module's docstring for why.

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_redis()

    # Warm critical caches on startup
    try:
        from routers.categories import warm_categories_cache
        await warm_categories_cache()
        logger.info("Cache warmed on startup ✅")
    except Exception as e:
        logger.warning(f"Cache warming failed (non-critical): {e}")

    logger.info("App started successfully")
    yield
    await close_redis()

app = FastAPI(title="clovical", lifespan=lifespan, docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── Global Exception Handler ──────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        f"Unhandled exception on {request.method} {request.url.path}: {exc}",
        exc_info=True
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. We are looking into it."}
    )


@app.get("/health")
async def health_check():
    """Lightweight health check for UptimeRobot and monitoring."""
    status = {
        "app": "ok",
        "redis": "unknown",
        "db": "unknown"
    }
    # Check Redis
    try:
        from utils.cache import redis_client
        if redis_client:
            await redis_client.ping()
            status["redis"] = "ok"
        else:
            status["redis"] = "disabled"
    except Exception as e:
        status["redis"] = f"error: {str(e)}"
        logger.warning(f"Redis health check failed: {e}")

    # Check Supabase DB
    try:
        await run_query(supabase_admin.table("admins").select("id").limit(1))
        status["db"] = "ok"
    except Exception as e:
        status["db"] = f"error: {str(e)}"
        logger.error(f"DB health check failed: {e}", exc_info=True)

    # Return 200 if app is running even if Redis/DB have issues
    return status

ALLOWED_ORIGINS = [
    "https://www.clovical.in",
    "https://clovical.in",
    "https://clovical.up.railway.app",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
# Allow an extra origin (e.g. a staging URL) to be added per-deploy without a
# code change.
_extra_origin = os.getenv("EXTRA_ALLOWED_ORIGIN")
if _extra_origin and _extra_origin not in ALLOWED_ORIGINS:
    ALLOWED_ORIGINS.append(_extra_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
    allow_credentials=True,
)

# ─── Middleware: Visitor Tracking + Bot Protection ─────────────────────────
@app.middleware("http")
async def track_and_protect(request: Request, call_next):
    client_ip = request.headers.get("CF-Connecting-IP") or get_remote_address(request)
    ip_hash = hashlib.sha256(f"{HASH_SALT}{client_ip}".encode()).hexdigest()

    # Block IPs (Redis-backed — see utils/login_throttle.py)
    if await login_ip_blocked(client_ip):
        return JSONResponse({"detail": "Too many failed attempts"}, status_code=429)

    # Log visitors (only customer pages, not static/api/admin).
    # Fire-and-forget: this is best-effort analytics, not something the
    # customer's page render should ever wait on. asyncio.create_task lets
    # the insert happen in the background while call_next proceeds below.
    path = request.url.path
    if (not path.startswith("/static")
            and not path.startswith("/api")
            and not path.startswith("/admin")):
        async def _log_visitor():
            try:
                await run_query(
                    supabase_admin.table("visitors").insert({
                        "page": path,
                        "ip_hash": ip_hash
                    })
                )
            except Exception:
                pass
        asyncio.create_task(_log_visitor())

        async def _track_active():
            try:
                from utils.cache import redis_client
                if redis_client:
                    active_key = f"active_visitor:{ip_hash}"
                    await redis_client.setex(active_key, 300, "1")
            except Exception:
                pass  # Never break main flow
        asyncio.create_task(_track_active())

    start = time.time()
    response = await call_next(request)
    duration = time.time() - start

    if duration > 2:
        logger.warning(f"Slow Supabase/response: {path} took {duration:.2f}s")

    # Request logging
    cache_tag = " [CACHE HIT]" if response.headers.get("X-Cache") == "HIT" else ""
    log_line = f"{request.method} {path} → {response.status_code} ({duration:.2f}s){cache_tag}"
    if response.status_code >= 500:
        logger.error(log_line)
    elif response.status_code >= 400:
        logger.warning(log_line)
    else:
        logger.info(log_line)

    return response

# ─── Static Files & Templates ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")
from utils.asset_version import ASSET_VERSION
templates.env.globals["ASSET_V"] = ASSET_VERSION

# ─── Routers ──────────────────────────────────────────────────────────────
app.include_router(public.router)
app.include_router(auth.router, prefix="/api/auth")
app.include_router(product_requests.router, prefix="/api/requests")
app.include_router(orders.router, prefix="/api/orders")
app.include_router(shopkeepers.router, prefix="/api/shopkeepers")
app.include_router(admin.router, prefix="/api/admin")
app.include_router(analytics.router, prefix="/api/analytics")
app.include_router(categories.router, prefix="/api/categories")
app.include_router(shopkeeper_auth.router, prefix="/api/shopkeeper-auth")
app.include_router(shopkeeper_panel.router, prefix="/api/shopkeeper")
app.include_router(products.router, prefix="/api/products")
app.include_router(customer_auth.router, prefix="/api/customer-auth")
app.include_router(payments.router, prefix="/api/payments/cashfree")
app.include_router(notifications.router, prefix="/api/notifications")
app.include_router(cart.router, prefix="/api/cart")