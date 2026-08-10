import json
import logging
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from utils.db import supabase_admin, run_query, run_blocking
from utils.auth_utils import require_admin, require_shopkeeper
from utils.image_utils import compress_and_thumbnail, compress_to_webp
from utils.cache import (
    cache_get, cache_set, cache_clear_pattern,
    two_layer_get, two_layer_set, two_layer_clear_pattern,
)

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB client-side cap (server still re-encodes/caps dimensions regardless)
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
    stock: int = Form(1),
    size_stock: str = Form("{}"),
    color_stock: str = Form("{}"),
    size_chart: str = Form(None),
    image: UploadFile = File(...),
    shopkeeper=Depends(require_shopkeeper),
):
    try:
        contents = await image.read()
        if len(contents) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=400, detail="Image exceeds 10MB limit. Please choose a smaller photo.")

        try:
            full_bytes, thumb_bytes = await run_blocking(compress_and_thumbnail, contents)
        except Exception as e:
            logger.error(f"Image compression failed for shopkeeper {shopkeeper['shopkeeper_id']}: {e}", exc_info=True)
            raise HTTPException(status_code=400, detail="Could not process that image — please try a different photo.")

        prefix = f"requests/{shopkeeper['shopkeeper_id']}"
        full_url = await _upload_bytes(full_bytes, "webp", "image/webp", prefix)
        thumb_url = await _upload_bytes(thumb_bytes, "webp", "image/webp", f"{prefix}/thumb")

        try:
            parsed_size_stock = json.loads(size_stock) if size_stock else {}
            parsed_size_stock = parsed_size_stock if isinstance(parsed_size_stock, dict) else {}
        except Exception:
            parsed_size_stock = {}
        try:
            parsed_color_stock = json.loads(color_stock) if color_stock else {}
            parsed_color_stock = parsed_color_stock if isinstance(parsed_color_stock, dict) else {}
        except Exception:
            parsed_color_stock = {}
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
            "stock": stock,
            "shopkeeper_image": full_url,
            "shopkeeper_image_thumb": thumb_url,
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
            .select("id,name,category,gender,shopkeeper_price,stock,status,shopkeeper_image_thumb,created_at,reviewed_at")
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


@router.put("/admin/{request_id}")
async def update_request(
    request_id: str,
    margin: float = Form(None),
    mrp: float = Form(None),
    admin_image: UploadFile = File(None),
    admin=Depends(require_admin),
):
    """Lets the admin set margin/MRP and attach an AI-generated replacement
    image, ahead of accepting the request. Can be called multiple times
    (e.g. to swap out the AI image) before /accept is called."""
    try:
        existing = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        if not existing.data:
            raise HTTPException(status_code=404, detail="Request not found")
        if existing.data["status"] != "pending":
            raise HTTPException(status_code=400, detail="Only pending requests can be edited")

        updates = {}
        if margin is not None:
            updates["margin"] = margin
        if mrp is not None:
            updates["mrp"] = mrp if mrp > 0 else None

        if admin_image and admin_image.filename:
            contents = await admin_image.read()
            if len(contents) > MAX_IMAGE_SIZE:
                raise HTTPException(status_code=400, detail="Image exceeds 10MB limit.")
            try:
                webp_bytes = await run_blocking(compress_to_webp, contents)
            except Exception as e:
                logger.error(f"AI image compression failed for request {request_id}: {e}", exc_info=True)
                raise HTTPException(status_code=400, detail="Could not process that image.")

            # Replace any previously-attached AI image (hard delete the old one)
            if existing.data.get("admin_image"):
                await _delete_storage_files([existing.data["admin_image"]])

            url = await _upload_bytes(webp_bytes, "webp", "image/webp", "generated")
            updates["admin_image"] = url

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
    AI-generated image as the product's single stored image. The
    shopkeeper's originally uploaded photo (full + thumb) is permanently
    deleted from storage at this point — only the AI-generated image
    remains anywhere.
    """
    try:
        res = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        r = res.data
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")
        if r["status"] != "pending":
            raise HTTPException(status_code=400, detail="Request has already been reviewed")
        if r.get("margin") is None:
            raise HTTPException(status_code=400, detail="Set a margin before accepting")
        if not r.get("admin_image"):
            raise HTTPException(status_code=400, detail="Attach an AI-generated image before accepting")

        sk = await run_query(supabase_admin.table("shopkeepers").select("id").eq("id", r["shopkeeper_id"]).single())
        if not sk.data:
            raise HTTPException(status_code=404, detail="Shopkeeper no longer exists")
        shopkeeper_code = f"#{sk.data['id']:03d}"

        our_price = round(r["shopkeeper_price"] + r["margin"], 2)
        product = {
            "name": r["name"],
            "description": r.get("description") or "",
            "our_price": our_price,
            "shopkeeper_price": r["shopkeeper_price"],
            "mrp": r.get("mrp"),
            "sizes": r.get("sizes") or [],
            "colors": r.get("colors") or [],
            "size_stock": r.get("size_stock") or {},
            "color_stock": r.get("color_stock") or {},
            "size_chart": r.get("size_chart"),
            "category": r.get("category"),
            "gender": r.get("gender"),
            "featured": False,
            "stock": r.get("stock") or 1,
            "shopkeeper_id": r["shopkeeper_id"],
            "shopkeeper_code": shopkeeper_code,
            "image": r["admin_image"],
            "images": [r["admin_image"]],
        }
        prod_res = await run_query(supabase_admin.table("products").insert(product))
        new_product = prod_res.data[0]

        # Hard-delete the shopkeeper's original upload — only the AI image survives.
        await _delete_storage_files([r.get("shopkeeper_image"), r.get("shopkeeper_image_thumb")])

        await run_query(supabase_admin.table("product_requests").update({
            "status": "accepted",
            "product_id": new_product["id"],
            "shopkeeper_image": None,
            "shopkeeper_image_thumb": None,
            "reviewed_at": "now()",
        }).eq("id", request_id))

        await cache_clear_pattern("requests:admin:*")
        await cache_clear_pattern("products:*")
        await two_layer_clear_pattern("products:filter-options:")
        await two_layer_clear_pattern(f"requests:mine:{r['shopkeeper_id']}")
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
    associated image file (shopkeeper's original + any admin-attached AI
    image) are hard-deleted so nothing is left orphaned."""
    try:
        res = await run_query(supabase_admin.table("product_requests").select("*").eq("id", request_id).single())
        r = res.data
        if not r:
            raise HTTPException(status_code=404, detail="Request not found")

        await _delete_storage_files([
            r.get("shopkeeper_image"),
            r.get("shopkeeper_image_thumb"),
            r.get("admin_image"),
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