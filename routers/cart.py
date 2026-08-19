"""
routers/cart.py — Customer cart/wishlist server sync
======================================================
Persists a logged-in customer's cart and wishlist server-side so they
survive a cleared browser or a switch of devices. Backed by the
`customer_carts` table (see schema_customer_cart.sql — one row per
customer, PK customer_id, JSONB cart/wishlist columns).

Guests never hit this router — they stay on localStorage only (see
static/js/shared.js's Cart/Wishlist objects). CartSync in that same file
is the frontend counterpart: on page load it calls GET /mine and merges
the server copy into localStorage, then after every local change it
debounces a PUT /mine to push the merged state back up.

Mounted at /api/cart in main.py.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Any

from utils.db import supabase_admin, run_query
from utils.auth_utils import require_customer

logger = logging.getLogger(__name__)
router = APIRouter()


class CartSyncRequest(BaseModel):
    cart: List[Any] = Field(default_factory=list)
    wishlist: List[Any] = Field(default_factory=list)


@router.get("/mine")
async def get_my_cart(customer=Depends(require_customer)):
    """Return this customer's server-side cart/wishlist (empty lists if none saved yet)."""
    try:
        res = await run_query(
            supabase_admin.table("customer_carts")
            .select("cart, wishlist")
            .eq("customer_id", customer["sub"])
            .maybe_single()
        )
        row = res.data if res else None
        return {
            "cart": (row or {}).get("cart") or [],
            "wishlist": (row or {}).get("wishlist") or [],
        }
    except Exception as e:
        logger.error(f"Failed to fetch cart for customer {customer['sub']}: {e}", exc_info=True)
        # Fail soft — frontend already has localStorage as the source of
        # truth for this browser, so an empty server copy just means the
        # merge is a no-op rather than breaking the page.
        return {"cart": [], "wishlist": []}


@router.put("/mine")
async def save_my_cart(payload: CartSyncRequest, customer=Depends(require_customer)):
    """Upsert this customer's cart/wishlist. Called (debounced) after every local change."""
    try:
        await run_query(
            supabase_admin.table("customer_carts").upsert({
                "customer_id": customer["sub"],
                "cart": payload.cart,
                "wishlist": payload.wishlist,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        )
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to save cart for customer {customer['sub']}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to sync cart")