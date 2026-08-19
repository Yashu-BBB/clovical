"""
IP-based login-throttle / brute-force guard.

Shared by admin login (routers/auth.py) and shopkeeper login
(routers/shopkeeper_auth.py), and consulted on every request by the
`track_and_protect` middleware in main.py so a blocked IP is rejected
before it can hit any route.

Backed by Redis (via utils.cache.redis_client) so the block-list survives
restarts and is shared across instances. Falls back to an in-memory dict
if Redis is unavailable, so login protection still works in that case —
it just won't be shared across multiple instances/restarts.
"""
import time
import logging

from utils.cache import redis_client

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5          # failed attempts allowed before blocking
BLOCK_SECONDS = 15 * 60   # how long an IP stays blocked once it trips the limit
WINDOW_SECONDS = 15 * 60  # window in which failures are counted

_KEY_PREFIX = "login_throttle:"

# ─── In-memory fallback (used only if Redis is unavailable) ────────────────
_mem_failures: dict[str, list[float]] = {}
_mem_blocked_until: dict[str, float] = {}


def _key(ip: str) -> str:
    return f"{_KEY_PREFIX}{ip}"


async def is_blocked(ip: str) -> bool:
    """Return True if this IP is currently blocked from logging in."""
    if redis_client:
        try:
            val = await redis_client.get(f"{_key(ip)}:blocked")
            return val is not None
        except Exception as e:
            logger.error(f"login_throttle.is_blocked redis error: {e}", exc_info=True)
            # fall through to in-memory check

    until = _mem_blocked_until.get(ip)
    if until is None:
        return False
    if time.time() >= until:
        _mem_blocked_until.pop(ip, None)
        return False
    return True


async def record_failure(ip: str, context: str = "") -> None:
    """Record a failed login attempt for this IP; block it if it hits the limit."""
    if redis_client:
        try:
            count_key = f"{_key(ip)}:count"
            count = await redis_client.incr(count_key)
            if count == 1:
                await redis_client.expire(count_key, WINDOW_SECONDS)
            if count >= MAX_ATTEMPTS:
                await redis_client.setex(f"{_key(ip)}:blocked", BLOCK_SECONDS, "1")
                logger.warning(f"IP blocked after {count} failed attempts ({context}): {ip}")
            return
        except Exception as e:
            logger.error(f"login_throttle.record_failure redis error: {e}", exc_info=True)
            # fall through to in-memory tracking

    now = time.time()
    attempts = [t for t in _mem_failures.get(ip, []) if now - t < WINDOW_SECONDS]
    attempts.append(now)
    _mem_failures[ip] = attempts
    if len(attempts) >= MAX_ATTEMPTS:
        _mem_blocked_until[ip] = now + BLOCK_SECONDS
        logger.warning(f"IP blocked after {len(attempts)} failed attempts ({context}): {ip}")


async def clear(ip: str) -> None:
    """Clear failure history / block state for this IP (called on successful login)."""
    if redis_client:
        try:
            await redis_client.delete(f"{_key(ip)}:count", f"{_key(ip)}:blocked")
            return
        except Exception as e:
            logger.error(f"login_throttle.clear redis error: {e}", exc_info=True)
            # fall through to in-memory cleanup

    _mem_failures.pop(ip, None)
    _mem_blocked_until.pop(ip, None)