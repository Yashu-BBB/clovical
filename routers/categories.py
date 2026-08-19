import logging
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from utils.db import supabase_admin, run_query
from utils.auth_utils import require_admin
from utils.cache import two_layer_get, two_layer_set, two_layer_clear_pattern

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


class CategoryCreate(BaseModel):
    name: str = Field(..., max_length=100)
    icon: str = Field("🏷️", max_length=10)
    gender: str = Field(..., max_length=10)
    sort_order: int = Field(0, ge=0, le=1000)


class CategoryUpdate(BaseModel):
    name: str | None = None
    icon: str | None = None
    gender: str | None = None
    sort_order: int | None = None


async def _find_similar_category(name: str, exclude_id: int | None = None) -> dict | None:
    """
    Catches near-duplicate categories the DB's UNIQUE(name) constraint
    can't: that constraint is an exact, case-sensitive string match, so
    "Hoodie" / "Hoodie " / "hoodie" / "HOODIE" are all distinct rows as
    far as Postgres is concerned even though they're the same category to
    an admin (and to every exact-string category match elsewhere in the
    app — the storefront filters, the admin edit-product category
    dropdown's pre-select, etc.). Compares trimmed + casefolded names so
    add_category()/update_category() below can reject that whole class of
    near-duplicate before it ever reaches the DB.
    """
    normalized = name.strip().casefold()
    if not normalized:
        return None
    res = await run_query(supabase_admin.table("categories").select("id,name,gender"))
    for cat in (res.data or []):
        if exclude_id is not None and cat.get("id") == exclude_id:
            continue
        if (cat.get("name") or "").strip().casefold() == normalized:
            return cat
    return None


# ─── PUBLIC ───────────────────────────────────────────────────────────────

@router.get("/")
@limiter.limit("60/minute")
async def list_categories(request: Request, gender: str | None = None):
    cache_key = f"categories:all:{gender}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        q = supabase_admin.table("categories").select("*").order("sort_order")
        if gender:
            q = q.eq("gender", gender)
        res = await run_query(q)
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=3600, mem_ttl=1800)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch categories: {e}", exc_info=True)
        return []


@router.get("/boys")
@limiter.limit("60/minute")
async def boys_categories(request: Request):
    cache_key = "categories:boys"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(supabase_admin.table("categories").select("*").eq("gender", "Boys").order("sort_order"))
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=3600, mem_ttl=1800)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch boys categories: {e}", exc_info=True)
        return []


@router.get("/girls")
@limiter.limit("60/minute")
async def girls_categories(request: Request):
    cache_key = "categories:girls"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(supabase_admin.table("categories").select("*").eq("gender", "Girls").order("sort_order"))
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=3600, mem_ttl=1800)
        return data
    except Exception as e:
        logger.error(f"Failed to fetch girls categories: {e}", exc_info=True)
        return []


async def warm_categories_cache():
    """Pre-warm categories cache on app startup."""
    for gender in ["Boys", "Girls", None]:
        q = supabase_admin.table("categories").select("*").order("sort_order")
        if gender:
            q = q.eq("gender", gender)
        res = await run_query(q)
        data = res.data or []
        key = f"categories:all:{gender}"
        await two_layer_set(key, data, redis_ttl=3600, mem_ttl=1800)
    logger.info("Categories cache warmed ✅")


# ─── ADMIN ────────────────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_list_categories(admin=Depends(require_admin)):
    try:
        res = await run_query(supabase_admin.table("categories").select("*").order("gender").order("sort_order"))
        return res.data or []
    except Exception as e:
        logger.error(f"Admin: failed to list categories: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch categories")


@router.post("/admin/add")
async def add_category(data: CategoryCreate, admin=Depends(require_admin)):
    if data.gender not in ("Boys", "Girls"):
        raise HTTPException(status_code=400, detail="Gender must be Boys or Girls")
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Category name is required")
    try:
        dup = await _find_similar_category(name)
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"A category named '{dup['name']}' (id {dup['id']}) already exists. "
                       f"Use that one instead of creating a near-duplicate."
            )
        res = await run_query(supabase_admin.table("categories").insert({
            "name": name,
            "icon": data.icon,
            "gender": data.gender,
            "sort_order": data.sort_order
        }))
        await two_layer_clear_pattern("categories:")
        logger.info(f"Category added: {name} by admin {admin['sub']}")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to add category: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add category")


@router.put("/admin/{cat_id}")
async def update_category(cat_id: int, data: CategoryUpdate, admin=Depends(require_admin)):
    try:
        updates = {k: v for k, v in data.dict().items() if v is not None}
        if "name" in updates:
            updates["name"] = updates["name"].strip()
            if not updates["name"]:
                raise HTTPException(status_code=400, detail="Category name is required")
            dup = await _find_similar_category(updates["name"], exclude_id=cat_id)
            if dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"A category named '{dup['name']}' (id {dup['id']}) already exists. "
                           f"Use that one instead of creating a near-duplicate."
                )
        res = await run_query(supabase_admin.table("categories").update(updates).eq("id", cat_id))
        await two_layer_clear_pattern("categories:")
        return res.data[0] if res.data else {}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update category: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update category")


@router.delete("/admin/{cat_id}")
async def delete_category(cat_id: int, admin=Depends(require_admin)):
    try:
        await run_query(supabase_admin.table("categories").delete().eq("id", cat_id))
        await two_layer_clear_pattern("categories:")
        return {"success": True}
    except Exception as e:
        logger.error(f"Failed to delete category: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete category")