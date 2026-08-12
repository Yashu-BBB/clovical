import logging
from pydantic import BaseModel
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from utils.db import supabase_admin, run_query
from utils.auth_utils import require_shopkeeper
from utils.cache import (
    two_layer_get, two_layer_set, two_layer_clear_pattern,
    cache_clear_pattern, mem_clear_pattern,
)
from utils.notifications import check_out_of_stock

logger = logging.getLogger(__name__)
router = APIRouter()

# Fields shown to the shopkeeper for their own live products. Deliberately
# excludes our_price/mrp/profit — the shopkeeper only ever sees the price
# they get paid (shopkeeper_price), never the margin on top of it.
SHOPKEEPER_PRODUCT_FIELDS = "id,name,category,gender,shopkeeper_price,sizes,colors,size_stock,color_stock,stock,image,created_at"

# Same fields, plus what's needed to render per-size/per-colour stock
# controls on the shopkeeper's own Stock page.
SHOPKEEPER_STOCK_FIELDS = "id,name,category,image,sizes,colors,stock,size_stock,color_stock"


@router.get("/me")
async def me(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"shopkeeper:me:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("shopkeepers").select("id,shop_name,shopkeeper_name,contact")
            .eq("id", shopkeeper["shopkeeper_id"]).single()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="Shopkeeper not found")
        data = {**res.data, "code": f"#{res.data['id']:03d}"}
        await two_layer_set(cache_key, data, redis_ttl=900, mem_ttl=120)
        return data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper profile: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch profile")


@router.get("/products")
async def my_products(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"shopkeeper:products:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("products").select(SHOPKEEPER_PRODUCT_FIELDS)
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("created_at", desc=True)
        )
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=300, mem_ttl=60)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper products: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch your products")


# ─── STOCK MANAGEMENT (shopkeeper's own live products only) ────────────────
# Mirrors the admin stock-list / stock-update endpoints in routers/products.py,
# but scoped to the requesting shopkeeper's own products only — a shopkeeper
# can never see or touch another shopkeeper's stock. Text/number only, no
# image handling, so this stays lightweight even on a phone connection.

@router.get("/stock-list")
async def my_stock_list(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"shopkeeper:stock-list:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("products").select(SHOPKEEPER_STOCK_FIELDS)
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("name")
        )
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=300, mem_ttl=30)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper stock list: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch your stock")


class ShopkeeperStockUpdate(BaseModel):
    stock: Optional[int] = None
    size_stock: Optional[dict] = None
    color_stock: Optional[dict] = None


async def _clear_shopkeeper_and_public_caches(shopkeeper_id: int):
    await two_layer_clear_pattern(f"shopkeeper:stock-list:{shopkeeper_id}")
    await two_layer_clear_pattern(f"shopkeeper:products:{shopkeeper_id}")
    await cache_clear_pattern("products:*")
    await two_layer_clear_pattern("products:filter-options:")
    mem_clear_pattern("product:")


@router.put("/stock/{product_id}")
async def update_my_stock(product_id: str, data: ShopkeeperStockUpdate, shopkeeper=Depends(require_shopkeeper)):
    """Add/reduce stock (overall and/or per size/colour) on one of the
    shopkeeper's own live products. Ownership is verified before any write —
    a shopkeeper can never modify a product that isn't theirs."""
    try:
        owner_check = await run_query(
            supabase_admin.table("products").select("id,name,shopkeeper_id,stock,size_stock,color_stock").eq("id", product_id).single()
        )
        if not owner_check.data:
            raise HTTPException(status_code=404, detail="Product not found")
        if owner_check.data.get("shopkeeper_id") != shopkeeper["shopkeeper_id"]:
            raise HTTPException(status_code=403, detail="You can only manage stock for your own products")
        before_stock = owner_check.data

        updates = {}
        if data.stock is not None:
            updates["stock"] = max(0, data.stock)
        if data.size_stock is not None:
            updates["size_stock"] = {k: max(0, v) for k, v in data.size_stock.items()}
        if data.color_stock is not None:
            updates["color_stock"] = {k: max(0, v) for k, v in data.color_stock.items()}
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        res = await run_query(supabase_admin.table("products").update(updates).eq("id", product_id))
        await _clear_shopkeeper_and_public_caches(shopkeeper["shopkeeper_id"])
        logger.info(f"Shopkeeper {shopkeeper['shopkeeper_id']} updated stock for product {product_id}: {updates}")
        updated = res.data[0] if res.data else {}
        await check_out_of_stock(
            product_id, before_stock.get("name") or "Product",
            before_stock,
            {"stock": updated.get("stock"), "size_stock": updated.get("size_stock"), "color_stock": updated.get("color_stock")},
        )
        return updated
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update stock for {product_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update stock")