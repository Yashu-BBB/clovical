import logging
from fastapi import APIRouter, Depends, HTTPException
from utils.db import supabase_admin, run_query
from utils.auth_utils import require_shopkeeper
from utils.cache import two_layer_get, two_layer_set

logger = logging.getLogger(__name__)
router = APIRouter()

# Fields shown to the shopkeeper for their own live products. Deliberately
# excludes our_price/mrp/profit — the shopkeeper only ever sees the price
# they get paid (shopkeeper_price), never the margin on top of it.
SHOPKEEPER_PRODUCT_FIELDS = "id,name,category,gender,shopkeeper_price,sizes,colors,size_stock,color_stock,stock,image,created_at"


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