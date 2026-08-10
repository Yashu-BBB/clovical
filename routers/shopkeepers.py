import logging
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import require_admin, hash_password
from utils.nimbuspost import register_pickup_address
from utils.cache import cache_get, cache_set, two_layer_get, two_layer_set, two_layer_clear_pattern

logger = logging.getLogger(__name__)
router = APIRouter()


class ShopkeeperCreate(BaseModel):
    shop_name: str = Field(..., max_length=200)
    shopkeeper_name: str = Field(..., max_length=200)
    contact: str = Field(..., max_length=15)
    address: str | None = Field(None, max_length=500)
    pincode: str | None = Field(None, max_length=6, min_length=6)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)


class ShopkeeperUpdate(BaseModel):
    shop_name: str | None = None
    shopkeeper_name: str | None = None
    contact: str | None = None
    address: str | None = None
    pincode: str | None = None
    city: str | None = None
    state: str | None = None


class ShopkeeperCredentials(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)


def _maybe_register_pickup(shopkeeper: dict) -> str | None:
    """
    Registers/refreshes the NimbusPost pickup address for a shopkeeper if
    the address fields are present. Never raises — NimbusPost failures
    must not block shopkeeper create/update.
    """
    if not (shopkeeper.get("address") and shopkeeper.get("pincode")
            and shopkeeper.get("city") and shopkeeper.get("state")):
        return None
    try:
        return register_pickup_address(shopkeeper)
    except Exception as e:
        logger.error(f"NimbusPost pickup registration errored for shopkeeper {shopkeeper.get('id')}: {e}", exc_info=True)
        return None


@router.get("/admin/all")
async def list_shopkeepers(admin=Depends(require_admin)):
    cache_key = "shopkeepers:all"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached

        res = await run_query(supabase_admin.table("shopkeepers").select("*").order("id"))
        shopkeepers = res.data or []

        # Enrich with product/sold counts
        for sk in shopkeepers:
            code = f"#{sk['id']:03d}"
            prods = await run_query(supabase_admin.table("products").select("id", count="exact").eq("shopkeeper_code", code))
            sold = await run_query(supabase_admin.table("orders").select("id", count="exact").eq("shopkeeper_code", code).not_.eq("status", "cancelled"))
            sk["total_products"] = prods.count or 0
            sk["total_sold"] = sold.count or 0
            sk["code"] = code
            sk["has_login"] = bool(sk.get("username"))
            sk.pop("password", None)  # never leak the hash to the admin client

        await cache_set(cache_key, shopkeepers, ttl=900)
        return shopkeepers
    except Exception as e:
        logger.error(f"Failed to list shopkeepers: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch shopkeepers")


@router.post("/admin/add")
async def add_shopkeeper(data: ShopkeeperCreate, admin=Depends(require_admin)):
    try:
        res = await run_query(supabase_admin.table("shopkeepers").insert({
            "shop_name": data.shop_name,
            "shopkeeper_name": data.shopkeeper_name,
            "contact": data.contact,
            "address": data.address,
            "pincode": data.pincode,
            "city": data.city,
            "state": data.state,
        }))
        new_sk = res.data[0]
        logger.info(f"Shopkeeper added: {data.shop_name} by admin {admin['sub']}")

        # Auto-register NimbusPost pickup address (never blocks shopkeeper creation)
        pickup_id = await run_blocking(_maybe_register_pickup, new_sk)
        if pickup_id:
            await run_query(supabase_admin.table("shopkeepers").update({"nimbuspost_pickup_id": pickup_id}).eq("id", new_sk["id"]))
            new_sk["nimbuspost_pickup_id"] = pickup_id
        elif data.address:
            logger.warning(f"NimbusPost pickup registration failed/skipped for new shopkeeper {new_sk['id']}")

        await two_layer_clear_pattern("shopkeepers:")
        return new_sk
    except Exception as e:
        logger.error(f"Failed to add shopkeeper: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add shopkeeper")


@router.put("/admin/{sk_id}")
async def update_shopkeeper(sk_id: int, data: ShopkeeperUpdate, admin=Depends(require_admin)):
    try:
        updates = {k: v for k, v in data.dict().items() if v is not None}
        res = await run_query(supabase_admin.table("shopkeepers").update(updates).eq("id", sk_id))
        updated = res.data[0] if res.data else {}

        # Re-register pickup address if address fields changed
        if updated and any(k in updates for k in ("address", "pincode", "city", "state")):
            pickup_id = await run_blocking(_maybe_register_pickup, updated)
            if pickup_id:
                await run_query(supabase_admin.table("shopkeepers").update({"nimbuspost_pickup_id": pickup_id}).eq("id", sk_id))
                updated["nimbuspost_pickup_id"] = pickup_id
            else:
                logger.warning(f"NimbusPost pickup re-registration failed/skipped for shopkeeper {sk_id}")

        await two_layer_clear_pattern("shopkeepers:")
        return updated
    except Exception as e:
        logger.error(f"Failed to update shopkeeper: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update shopkeeper")


@router.delete("/admin/{sk_id}")
async def delete_shopkeeper(sk_id: int, admin=Depends(require_admin)):
    try:
        await run_query(supabase_admin.table("shopkeepers").delete().eq("id", sk_id))
        await two_layer_clear_pattern("shopkeepers:")
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to delete shopkeeper: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete shopkeeper")


@router.get("/admin/dropdown")
async def shopkeepers_dropdown(admin=Depends(require_admin)):
    """For product form dropdown."""
    cache_key = "shopkeepers:dropdown"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(supabase_admin.table("shopkeepers").select("id,shop_name").order("id"))
        data = [{"id": s["id"], "label": f"#{s['id']:03d} - {s['shop_name']}"} for s in (res.data or [])]
        await two_layer_set(cache_key, data, redis_ttl=900, mem_ttl=600)
        return data
    except Exception as e:
        logger.error(f"Shopkeeper dropdown failed: {e}", exc_info=True)
        return []


# ─── Shopkeeper Panel Login Credentials ────────────────────────────────────
# A shopkeeper only gets access to the Shopkeeper Panel once an admin sets a
# username/password for them here — there is no self-registration flow.

@router.put("/admin/{sk_id}/credentials")
async def set_shopkeeper_credentials(sk_id: int, data: ShopkeeperCredentials, admin=Depends(require_admin)):
    try:
        existing = await run_query(supabase_admin.table("shopkeepers").select("id").eq("id", sk_id).single())
        if not existing.data:
            raise HTTPException(status_code=404, detail="Shopkeeper not found")

        # Username must be unique across shopkeepers
        clash = await run_query(
            supabase_admin.table("shopkeepers").select("id").eq("username", data.username).neq("id", sk_id)
        )
        if clash.data:
            raise HTTPException(status_code=400, detail="That username is already taken")

        await run_query(supabase_admin.table("shopkeepers").update({
            "username": data.username,
            "password": hash_password(data.password),
        }).eq("id", sk_id))

        await two_layer_clear_pattern("shopkeepers:")
        logger.info(f"Shopkeeper panel credentials set for shopkeeper {sk_id} by admin {admin['sub']}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to set shopkeeper credentials for {sk_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to set credentials")


@router.delete("/admin/{sk_id}/credentials")
async def revoke_shopkeeper_credentials(sk_id: int, admin=Depends(require_admin)):
    """Revokes panel access without deleting the shopkeeper record itself."""
    try:
        await run_query(supabase_admin.table("shopkeepers").update({
            "username": None,
            "password": None,
        }).eq("id", sk_id))
        await two_layer_clear_pattern("shopkeepers:")
        logger.info(f"Shopkeeper panel credentials revoked for shopkeeper {sk_id} by admin {admin['sub']}")
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to revoke shopkeeper credentials for {sk_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to revoke credentials")