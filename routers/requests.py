import json
import logging
import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import require_admin, require_shopkeeper
from utils.image_utils import compress_and_thumbnail, compress_to_webp
from utils.cache import (
    cache_get, cache_set, cache_clear_pattern,
    two_layer_get, two_layer_set, two_layer_clear_pattern, mem_clear_pattern,
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB client-side cap (server still re-encodes/caps dimensions regardless)
MAX_ADMIN_IMAGES = 6               # same cap as the admin add-product form
BUCKET = "product-images"


def _storage_path_from_url(url: str) -> str | None:
    """Extracts the storage object path (everything after the bucket name in
    the public URL) so a stored image can be hard-deleted later. Returns
    None if the URL doesn't look like one of ours."""
    if not url or f"/{BUCKET}/" not in url:
        return None
    return url.split(f"/{BUCKET}/", 1)[1]


async def _delete_storage_files(urls: list[str]):
    """Best-effort hard delete of storage objects. Never raises — a failed
    storage cleanup should never block the DB operation that triggered it,
    but it is logged so orphaned files can be tracked down."""
    paths = [p for p in (_storage_path_from_url(u) for u in urls if u) if p]
    if not paths:
        return
    try:
        await run_blocking(supabase_admin.storage.from_(BUCKET).remove, paths)
        logger.info(f"Hard-deleted {len(paths)} storage file(s): {paths}")
    except Exception as e:
        logger.error(f"Failed to hard-delete storage files {paths}: {e}", exc_info=True)


async def _upload_bytes(data: bytes, ext: str, content_type: str, prefix: str) -> str:
    fname = f"{prefix}/{uuid.uuid4()}.{ext}"
    await run_blocking(
        supabase_admin.storage.from_(BUCKET).upload,
        fname, data, {"content-type": content_type}
    )
    return await run_blocking(supabase_admin.storage.from_(BUCKET).get_public_url, fname)


def _parse_json_dict(raw: str | None) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_json_list(raw: str | None) -> list:
    try:
        parsed = json.loads(raw) if raw else []
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


# ─── SHOPKEEPER-FACING ──────────────────────────────────────────────────────

@router.post("/submit")
async def submit_request(
    name: str = Form(..., max_length=200),
    description: str = Form("", max_length=2000),
    shopkeeper_price: float = Form(...),
    sizes: str = Form("[]"),
    colors: str = Form("[]"),
    category: str = Form("", max_length=100),
    gender: str = Form("Girls"),
    fabric: str = Form("", max_length=100),
    stock: int = Form(1),
    size_stock: str = Form("{}"),
    color_stock: str = Form("{}"),
    size_chart: str = Form(None),
    image_front: UploadFile = File(...),
    image_back: UploadFile = File(...),
    shopkeeper=Depends(require_shopkeeper),
):
    """
    Creates a pending product REQUEST (never a live product). Exactly two
    photos are required — front and back of the garment — matching the
    admin's own add-product format for every other field (sizes with per-
    size stock, colors with per-colour stock, size chart, fabric), but
    deliberately excluding our_price/MRP and any shopkeeper picker: the
    shopkeeper's identity comes from their login session only.
    """
    try:
        async def _process(upload: UploadFile, label: str) -> tuple[str, str]:
            contents = await upload.read()
            if len(contents) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail=f"{label} image exceeds 10MB limit. Please choose a smaller photo.")
            try:
                full_bytes, thumb_bytes = await run_blocking(compress_and_thumbnail, contents)
            except Exception as e:
                logger.error(f"Image compression failed ({label}) for shopkeeper {shopkeeper['shopkeeper_id']}: {e}", exc_info=True)
                raise HTTPException(status_code=400, detail=f"Could not process the {label.lower()} photo — please try a different image.")
            prefix = f"requests/{shopkeeper['shopkeeper_id']}"
            full_url = await _upload_bytes(full_bytes, "webp", "image/webp", prefix)
            thumb_url = await _upload_bytes(thumb_bytes, "webp", "image/webp", f"{prefix}/thumb")
            return full_url, thumb_url

        front_url, front_thumb = await _process(image_front, "Front")
        back_url, back_thumb = await _process(image_back, "Back")

        parsed_size_stock = _parse_json_dict(size_stock)
        parsed_color_stock = _parse_json_dict(color_stock)
        parsed_size_chart = None
        if size_chart and size_chart.strip():
            try:
                parsed_size_chart = json.loads(size_chart)
            except Exception:
                parsed_size_chart = None

        row = {
            "shopkeeper_id": shopkeeper["shopkeeper_id"],
            "name": name,
            "description": description,
            "shopkeeper_price": shopkeeper_price,
            "sizes": json.loads(sizes) if isinstance(sizes, str) else sizes,
            "colors": json.loads(colors) if isinstance(colors, str) else colors,
            "size_stock": parsed_size_stock,
            "color_stock": parsed_color_stock,
            "size_chart": parsed_size_chart,
            "category": category,
            "gender": gender,
            "fabric": fabric or None,
            "stock": stock,
            "shopkeeper_image_front": front_url,
            "shopkeeper_image_front_thumb": front_thumb,
            "shopkeeper_image_back": back_url,
            "shopkeeper_image_back_thumb": back_thumb,
            "status": "pending",
        }
        res = await run_query(supabase_admin.table("product_requests").insert(row))
        await two_layer_clear_pattern(f"requests:mine:{shopkeeper['shopkeeper_id']}")
        await cache_clear_pattern("requests:admin:*")
        logger.info(f"Product request submitted by shopkeeper {shopkeeper['shopkeeper_id']}: {name}")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit product request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit request")


@router.get("/mine")
async def my_requests(shopkeeper=Depends(require_shopkeeper)):
    cache_key = f"requests:mine:{shopkeeper['shopkeeper_id']}"
    try:
        cached = await two_layer_get(cache_key)
        if cached is not None:
            return cached
        res = await run_query(
            supabase_admin.table("product_requests")
            .select("id,name,category,gender,shopkeeper_price,stock,status,shopkeeper_image_front_thumb,created_at,reviewed_at")
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("created_at", desc=True)
        )
        data = res.data or []
        await two_layer_set(cache_key, data, redis_ttl=300, mem_ttl=60)
        return data
    except Exception as e:
        logger.error(f"Failed to list own requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch your requests")


# ─── ADMIN-FACING ───────────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_list_requests(status: str = "pending", admin=Depends(require_admin)):
    cache_key = f"requests:admin:{status}"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
        q = supabase_admin.table("product_requests").select("*").order("created_at", desc=True)
        if status and status != "all":
            q = q.eq("status", status)
        res = await run_query(q)
        data = res.data or []
        await cache_set(cache_key, data, ttl=120)
        return data
    except Exception as e:
        logger.error(f"Failed to list product requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch requests")


@router.get("/admin/{request_id}")
async def admin_get_request(request_id: str, admin=Depends(require_admin)):
    """Single request, full detail — used to open the review modal (which
    reuses the admin add-product modal, prefilled from this data)."""
    try:
        res = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        if not res.data:
            raise HTTPException(status_code=404, detail="Request not found")
        return res.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch request")


@router.put("/admin/{request_id}")
async def update_request(
    request_id: str,
    our_price: float = Form(None),
    mrp: float = Form(None),
    keep_images: str = Form("[]"),                    # JSON array of existing admin_images URLs to keep
    new_images: List[UploadFile] = File(default=[]),  # newly uploaded images (same uploader as admin add-product)
    admin=Depends(require_admin),
):
    """
    Lets the admin set our_price/MRP and attach/replace the listing image(s)
    ahead of accepting the request — same multi-image uploader (1–6 photos)
    as the admin add-product modal. Can be called multiple times (e.g. to
    reorder/swap images) before /accept is called.
    """
    try:
        existing = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        if not existing.data:
            raise HTTPException(status_code=404, detail="Request not found")
        if existing.data["status"] != "pending":
            raise HTTPException(status_code=400, detail="Only pending requests can be edited")

        updates = {}
        if our_price is not None:
            updates["our_price"] = our_price
        if mrp is not None:
            updates["mrp"] = mrp if mrp > 0 else None

        # Multi-image handling — identical pattern to the admin product editor.
        existing_urls = _parse_json_list(keep_images)
        current_images = existing.data.get("admin_images") or []
        removed_urls = [u for u in current_images if u not in existing_urls]

        valid_new = [img for img in new_images if img and img.filename]
        if len(existing_urls) + len(valid_new) > MAX_ADMIN_IMAGES:
            raise HTTPException(status_code=400, detail=f"Maximum {MAX_ADMIN_IMAGES} images allowed.")

        new_urls = []
        for img in valid_new:
            contents = await img.read()
            if len(contents) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail=f"Image '{img.filename}' exceeds 10MB limit.")
            try:
                webp_bytes = await run_blocking(compress_to_webp, contents)
            except Exception as e:
                logger.error(f"Admin image compression failed for request {request_id}: {e}", exc_info=True)
                raise HTTPException(status_code=400, detail="Could not process that image.")
            url = await _upload_bytes(webp_bytes, "webp", "image/webp", "listing")
            new_urls.append(url)

        if keep_images != "[]" or valid_new:
            all_images = (existing_urls + new_urls)[:MAX_ADMIN_IMAGES]
            updates["admin_images"] = all_images
            # Hard-delete any admin-attached images that were dropped from the set.
            await _delete_storage_files(removed_urls)

        if updates:
            await run_query(supabase_admin.table("product_requests").update(updates).eq("id", request_id))

        await cache_clear_pattern("requests:admin:*")
        logger.info(f"Request {request_id} updated by admin {admin['sub']}: {list(updates.keys())}")
        res = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        return res.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to update request")


@router.post("/admin/{request_id}/accept")
async def accept_request(request_id: str, admin=Depends(require_admin)):
    """
    Converts a pending request into a live product, using the admin's
    uploaded image(s) as the product's stored images — the same
    single-stored-image-per-product convention as products added directly
    by admin. The shopkeeper's original front/back photos (full + thumb,
    4 files total) are permanently deleted from storage at this point —
    only the admin's final listing image(s) remain, referenced by URL.
    """
    try:
        res = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        r = res.data
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")
        if r["status"] != "pending":
            raise HTTPException(status_code=400, detail="Request has already been reviewed")
        if r.get("our_price") is None:
            raise HTTPException(status_code=400, detail="Set Our Price before accepting")
        admin_images = r.get("admin_images") or []
        if not admin_images:
            raise HTTPException(status_code=400, detail="Attach at least one product image before accepting")

        sk = await run_query(supabase_admin.table("shopkeepers").select("id").eq("id", r["shopkeeper_id"]).single())
        if not sk.data:
            raise HTTPException(status_code=404, detail="Shopkeeper no longer exists")
        shopkeeper_code = f"#{sk.data['id']:03d}"

        product = {
            "name": r["name"],
            "description": r.get("description") or "",
            "our_price": r["our_price"],
            "shopkeeper_price": r["shopkeeper_price"],
            "mrp": r.get("mrp"),
            "sizes": r.get("sizes") or [],
            "colors": r.get("colors") or [],
            "size_stock": r.get("size_stock") or {},
            "color_stock": r.get("color_stock") or {},
            "size_chart": r.get("size_chart"),
            "category": r.get("category"),
            "gender": r.get("gender"),
            "fabric": r.get("fabric"),
            "featured": False,
            "stock": r.get("stock") or 1,
            "shopkeeper_id": r["shopkeeper_id"],
            "shopkeeper_code": shopkeeper_code,
            "image": admin_images[0],
            "images": admin_images,
        }
        prod_res = await run_query(supabase_admin.table("products").insert(product))
        new_product = prod_res.data[0]

        # Hard-delete the shopkeeper's original front/back uploads — only the
        # admin's final listing image(s) survive anywhere.
        await _delete_storage_files([
            r.get("shopkeeper_image_front"), r.get("shopkeeper_image_front_thumb"),
            r.get("shopkeeper_image_back"), r.get("shopkeeper_image_back_thumb"),
        ])

        await run_query(supabase_admin.table("product_requests").update({
            "status": "accepted",
            "product_id": new_product["id"],
            "shopkeeper_image_front": None,
            "shopkeeper_image_front_thumb": None,
            "shopkeeper_image_back": None,
            "shopkeeper_image_back_thumb": None,
            "reviewed_at": "now()",
        }).eq("id", request_id))

        await cache_clear_pattern("requests:admin:*")
        await cache_clear_pattern("products:*")
        await two_layer_clear_pattern("products:filter-options:")
        mem_clear_pattern("product:")
        await two_layer_clear_pattern(f"requests:mine:{r['shopkeeper_id']}")
        await two_layer_clear_pattern(f"shopkeeper:products:{r['shopkeeper_id']}")
        logger.info(f"Request {request_id} accepted by admin {admin['sub']} → product {new_product['id']}")
        return {"success": True, "product": new_product}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to accept request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to accept request")


@router.delete("/admin/{request_id}")
async def reject_request(request_id: str, admin=Depends(require_admin)):
    """Rejects and permanently deletes a request: the DB row AND every
    associated image file (shopkeeper's front/back originals + any
    admin-attached listing images) are hard-deleted so nothing is left
    orphaned in storage."""
    try:
        res = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        r = res.data
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")

        await _delete_storage_files([
            r.get("shopkeeper_image_front"), r.get("shopkeeper_image_front_thumb"),
            r.get("shopkeeper_image_back"), r.get("shopkeeper_image_back_thumb"),
            *(r.get("admin_images") or []),
        ])
        await run_query(supabase_admin.table("product_requests").delete().eq("id", request_id))

        await cache_clear_pattern("requests:admin:*")
        await two_layer_clear_pattern(f"requests:mine:{r['shopkeeper_id']}")
        logger.info(f"Request {request_id} rejected & hard-deleted by admin {admin['sub']}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reject request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reject request")