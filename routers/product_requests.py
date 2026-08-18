"""
Shopkeeper "request a product" feature.

Flow:
  1. Shopkeeper submits a request (POST /submit) with product details +
     two reference photos (front/back). Lands in `product_requests` with
     status="pending".
  2. Shopkeeper can see their own requests (GET /mine).
  3. Admin reviews pending requests (GET /admin/all), sets our_price/mrp
     and attaches real listing photos (PUT /admin/{id}) — same multi-image
     uploader as the add-product modal.
  4. Admin accepts (POST /admin/{id}/accept) -> a real row is inserted into
     `products` using the request's data + the admin's price/images, the
     shopkeeper's reference photos are deleted from storage, and the
     request row itself is deleted.
     OR admin rejects (DELETE /admin/{id}) -> reference photos deleted,
     request row deleted. Nothing goes live.

Mounted at /api/requests in main.py.
"""

import logging
import json
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import Optional, List

from utils.db import supabase_admin, run_query, run_blocking
from utils.cache import cache_clear_pattern, two_layer_clear_pattern, mem_clear_pattern
from utils.auth_utils import require_admin, require_shopkeeper
from utils.stock_utils import derive_total_stock
from utils.image_utils import compress_to_webp

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB per image
MAX_TOTAL_IMAGES = 6


async def _upload_image(img: UploadFile) -> str:
    contents = await img.read()
    if len(contents) > MAX_IMAGE_SIZE:
        raise HTTPException(status_code=400, detail=f"Image '{img.filename}' exceeds 5MB limit.")
    # Re-encode through Pillow server-side rather than trusting the client's
    # filename extension / Content-Type. This both normalizes storage to
    # WebP and doubles as content validation: anything that isn't actually
    # a decodable image raises here and is rejected with a 400, instead of
    # being uploaded as-is to public storage under a client-chosen type.
    try:
        webp_bytes = await run_blocking(compress_to_webp, contents)
    except Exception:
        raise HTTPException(status_code=400, detail=f"'{img.filename}' isn't a valid image file.")
    fname = f"{uuid.uuid4()}.webp"
    await run_blocking(
        supabase_admin.storage.from_("product-images").upload,
        fname, webp_bytes, {"content-type": "image/webp"}
    )
    return await run_blocking(supabase_admin.storage.from_("product-images").get_public_url, fname)


async def _delete_image(url: Optional[str]):
    """Best-effort delete of a stored image from its public URL. Never raises."""
    if not url:
        return
    try:
        fname = url.rsplit("/", 1)[-1]
        await run_blocking(supabase_admin.storage.from_("product-images").remove, [fname])
    except Exception as e:
        logger.warning(f"Failed to delete image {url}: {e}")


# ─── SHOPKEEPER ENDPOINTS ───────────────────────────────────────────────────

@router.post("/submit")
async def submit_request(
    name: str = Form(..., max_length=200),
    description: str = Form("", max_length=2000),
    fabric: str = Form("", max_length=100),
    shopkeeper_price: float = Form(...),
    sizes: str = Form("[]"),
    colors: str = Form("[]"),
    size_stock: str = Form("{}"),
    color_stock: str = Form("{}"),
    category: str = Form("", max_length=100),
    gender: str = Form("Girls"),
    size_chart: str = Form(None),
    image_front: UploadFile = File(...),
    image_back: UploadFile = File(...),
    shopkeeper=Depends(require_shopkeeper),
):
    try:
        try:
            parsed_size_stock = json.loads(size_stock) if size_stock else {}
            if not isinstance(parsed_size_stock, dict):
                parsed_size_stock = {}
        except Exception:
            parsed_size_stock = {}
        try:
            parsed_color_stock = json.loads(color_stock) if color_stock else {}
            if not isinstance(parsed_color_stock, dict):
                parsed_color_stock = {}
        except Exception:
            parsed_color_stock = {}

        derived_stock = derive_total_stock(parsed_size_stock, fallback=1)

        parsed_size_chart = None
        if size_chart and size_chart.strip():
            try:
                parsed_size_chart = json.loads(size_chart)
            except Exception:
                parsed_size_chart = None

        front_url = await _upload_image(image_front)
        back_url = await _upload_image(image_back)

        request_row = {
            "shopkeeper_id": shopkeeper["shopkeeper_id"],
            "name": name,
            "description": description,
            "fabric": fabric or None,
            "shopkeeper_price": shopkeeper_price,
            "sizes": json.loads(sizes) if isinstance(sizes, str) else sizes,
            "colors": json.loads(colors) if isinstance(colors, str) else colors,
            "size_stock": parsed_size_stock,
            "color_stock": parsed_color_stock,
            "category": category,
            "gender": gender,
            "size_chart": parsed_size_chart,
            "stock": derived_stock,
            "shopkeeper_image_front": front_url,
            "shopkeeper_image_back": back_url,
            "our_price": None,
            "mrp": None,
            "admin_images": [],
            "status": "pending",
        }
        res = await run_query(supabase_admin.table("product_requests").insert(request_row))
        logger.info(f"Product request submitted: {name} by shopkeeper {shopkeeper['shopkeeper_id']}")
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit product request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit request")


@router.get("/mine")
async def my_requests(shopkeeper=Depends(require_shopkeeper)):
    try:
        res = await run_query(
            supabase_admin.table("product_requests").select("*")
            .eq("shopkeeper_id", shopkeeper["shopkeeper_id"])
            .order("created_at", desc=True)
        )
        return res.data or []
    except Exception as e:
        logger.error(f"Failed to fetch shopkeeper's requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch requests")


# ─── ADMIN ENDPOINTS ─────────────────────────────────────────────────────

@router.get("/admin/all")
async def admin_list_requests(status: Optional[str] = "pending", admin=Depends(require_admin)):
    try:
        query = supabase_admin.table("product_requests").select("*").order("created_at", desc=True)
        if status and status != "all":
            query = query.eq("status", status)
        res = await run_query(query)
        return res.data or []
    except Exception as e:
        logger.error(f"Admin: failed to list requests: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch requests")


@router.put("/admin/{request_id}")
async def update_request(
    request_id: str,
    our_price: float = Form(None),
    mrp: float = Form(None),
    keep_images: str = Form("[]"),
    new_images: List[UploadFile] = File(default=[]),
    admin=Depends(require_admin),
):
    try:
        existing = await run_query(
            supabase_admin.table("product_requests").select("*").eq("id", request_id).single()
        )
        if not existing.data:
            raise HTTPException(status_code=404, detail="Request not found")

        try:
            keep_urls = json.loads(keep_images) if keep_images else []
            if not isinstance(keep_urls, list):
                keep_urls = []
        except Exception:
            keep_urls = []

        valid_new = [img for img in new_images if img and img.filename]
        if len(keep_urls) + len(valid_new) > MAX_TOTAL_IMAGES:
            raise HTTPException(status_code=400, detail=f"Maximum {MAX_TOTAL_IMAGES} images allowed.")

        # Delete any previously-attached images that were dropped
        old_images = existing.data.get("admin_images") or []
        for url in old_images:
            if url not in keep_urls:
                await _delete_image(url)

        new_urls = [await _upload_image(img) for img in valid_new]
        admin_images = keep_urls + new_urls

        update = {"admin_images": admin_images}
        if our_price is not None:
            update["our_price"] = our_price
        if mrp is not None:
            update["mrp"] = mrp

        res = await run_query(
            supabase_admin.table("product_requests").update(update).eq("id", request_id)
        )
        return res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to save changes")


@router.post("/admin/{request_id}/accept")
async def accept_request(request_id: str, admin=Depends(require_admin)):
    try:
        existing = await run_query(
            supabase_admin.table("product_requests").select("*").eq("id", request_id).single()
        )
        if not existing.data:
            raise HTTPException(status_code=404, detail="Request not found")
        r = existing.data

        if r.get("our_price") is None:
            raise HTTPException(status_code=400, detail="Set Our Price before accepting")
        if not r.get("admin_images"):
            raise HTTPException(status_code=400, detail="Attach at least one product image before accepting")

        sk = await run_query(
            supabase_admin.table("shopkeepers").select("id").eq("id", r["shopkeeper_id"]).single()
        )
        if not sk.data:
            raise HTTPException(status_code=404, detail="Shopkeeper not found")
        shopkeeper_code = f"#{sk.data['id']:03d}"

        image_urls = r.get("admin_images") or []
        product = {
            "name": r["name"],
            "description": r.get("description"),
            "our_price": r["our_price"],
            "shopkeeper_price": r["shopkeeper_price"],
            "sizes": r.get("sizes") or [],
            "colors": r.get("colors") or [],
            "category": r.get("category"),
            "gender": r.get("gender"),
            "fabric": r.get("fabric"),
            "mrp": r.get("mrp"),
            "featured": False,
            "stock": r.get("stock") or 0,
            "size_stock": r.get("size_stock") or {},
            "color_stock": r.get("color_stock") or {},
            "shopkeeper_id": r["shopkeeper_id"],
            "shopkeeper_code": shopkeeper_code,
            "image": image_urls[0] if image_urls else None,
            "images": image_urls,
            "size_chart": r.get("size_chart"),
        }
        insert_res = await run_query(supabase_admin.table("products").insert(product))

        # Clean up the shopkeeper's reference photos — not needed once live
        await _delete_image(r.get("shopkeeper_image_front"))
        await _delete_image(r.get("shopkeeper_image_back"))

        await run_query(supabase_admin.table("product_requests").delete().eq("id", request_id))

        await cache_clear_pattern("products:*")
        await two_layer_clear_pattern("products:filter-options:")
        mem_clear_pattern("product:")

        logger.info(f"Request {request_id} accepted -> product live, by admin {admin['sub']}")
        return insert_res.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to accept request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to accept request")


@router.delete("/admin/{request_id}")
async def reject_request(request_id: str, admin=Depends(require_admin)):
    try:
        existing = await run_query(
            supabase_admin.table("product_requests").select("*").eq("id", request_id).single()
        )
        if not existing.data:
            raise HTTPException(status_code=404, detail="Request not found")
        r = existing.data

        await _delete_image(r.get("shopkeeper_image_front"))
        await _delete_image(r.get("shopkeeper_image_back"))
        for url in (r.get("admin_images") or []):
            await _delete_image(url)

        await run_query(supabase_admin.table("product_requests").delete().eq("id", request_id))
        logger.info(f"Request {request_id} rejected & deleted by admin {admin['sub']}")
        return {"detail": "Request rejected and deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to reject request {request_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to reject request")