import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from utils.auth_utils import get_admin_from_request, get_shopkeeper_from_request
from utils.nimbuspost import is_configured as nimbuspost_is_configured
from utils.db import supabase_admin, run_query
from utils.asset_version import ASSET_VERSION

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["ASSET_V"] = ASSET_VERSION

SITE_URL = "https://clovical.in"


def render(template: str, request: Request, **ctx):
    # `site_url` + `canonical_url` are injected for every page so templates
    # can build a correct canonical/OG URL off one source of truth (SITE_URL)
    # instead of hardcoding the domain in each .html file. Query params are
    # deliberately dropped from the canonical (filters/search/sort on listing
    # pages shouldn't create separate indexable URLs).
    ctx.setdefault("site_url", SITE_URL)
    ctx.setdefault("canonical_url", f"{SITE_URL}{request.url.path}")
    return templates.TemplateResponse(template, {"request": request, **ctx})


def _ld_json(data: dict) -> str:
    """Serialize a dict to a JSON-LD-safe string for embedding inside a
    <script type="application/ld+json"> block. Escapes '</' so a value
    can never prematurely close the surrounding script tag."""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


# ─── SEO: sitemap.xml & robots.txt ─────────────────────────────────────────

def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


@router.get("/sitemap.xml")
async def sitemap_xml():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Static, always-indexable pages
    static_urls = [
        {"loc": f"{SITE_URL}/", "changefreq": "daily", "priority": "1.0", "lastmod": today},
        {"loc": f"{SITE_URL}/products", "changefreq": "daily", "priority": "0.9", "lastmod": today},
        {"loc": f"{SITE_URL}/products?gender=Girls", "changefreq": "daily", "priority": "0.8", "lastmod": today},
        {"loc": f"{SITE_URL}/products?gender=Boys", "changefreq": "daily", "priority": "0.8", "lastmod": today},
        {"loc": f"{SITE_URL}/privacy-policy", "changefreq": "yearly", "priority": "0.3", "lastmod": today},
        {"loc": f"{SITE_URL}/terms-and-conditions", "changefreq": "yearly", "priority": "0.3", "lastmod": today},
        {"loc": f"{SITE_URL}/cookie-policy", "changefreq": "yearly", "priority": "0.3", "lastmod": today},
    ]

    # Dynamic product pages, pulled live from the DB so the sitemap
    # never goes stale as products are added/removed.
    product_urls = []
    try:
        result = await run_query(
            supabase_admin.table("products")
            .select("id,created_at,stock")
            .gt("stock", 0)
        )
        for row in (result.data or []):
            pid = row.get("id")
            if not pid:
                continue
            created = row.get("created_at")
            lastmod = created[:10] if created else today
            product_urls.append({
                "loc": f"{SITE_URL}/product/{pid}",
                "changefreq": "weekly",
                "priority": "0.7",
                "lastmod": lastmod,
            })
    except Exception:
        # Never let a DB hiccup break the sitemap response — ship the
        # static pages at minimum.
        pass

    # Dynamic category listing pages (/products?category=...&gender=...),
    # also pulled live so new categories show up automatically.
    category_urls = []
    try:
        result = await run_query(
            supabase_admin.table("categories").select("name,gender")
        )
        for row in (result.data or []):
            name = row.get("name")
            gender = row.get("gender")
            if not name or not gender:
                continue
            qs = urlencode({"category": name, "gender": gender})
            category_urls.append({
                "loc": f"{SITE_URL}/products?{qs}",
                "changefreq": "weekly",
                "priority": "0.6",
                "lastmod": today,
            })
    except Exception:
        pass

    all_urls = static_urls + category_urls + product_urls

    body = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in all_urls:
        body.append(
            "  <url>"
            f"<loc>{_xml_escape(u['loc'])}</loc>"
            f"<lastmod>{u['lastmod']}</lastmod>"
            f"<changefreq>{u['changefreq']}</changefreq>"
            f"<priority>{u['priority']}</priority>"
            "</url>"
        )
    body.append("</urlset>")

    return Response(content="\n".join(body), media_type="application/xml")


@router.get("/robots.txt")
async def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin",
        "Disallow: /shopkeeper",
        "Disallow: /cart",
        "Disallow: /checkout",
        "Disallow: /my-orders",
        "Disallow: /wishlist",
        "Disallow: /order/confirmation",
        "Disallow: /api/",
        "",
        f"Sitemap: {SITE_URL}/sitemap.xml",
    ]
    return Response(content="\n".join(lines), media_type="text/plain")


# ─── Favicon at root scope ──────────────────────────────────────────────
# The file physically lives at static/images/favicon.ico, but browsers and
# crawlers (including Google's) request /favicon.ico at the site root
# directly, regardless of what the <link rel="icon"> tags in <head> say.
# Serving it here as well as via /static ensures that root-level request
# resolves instead of 404ing.
@router.get("/favicon.ico")
async def favicon_ico():
    with open("static/images/favicon.ico", "rb") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=604800"},
    )


# ─── Push notifications: service worker at root scope ─────────────────────
# The file physically lives at static/sw.js, but a service worker can only
# control paths at or below the URL it's served from. Serving it here at
# /sw.js (with Service-Worker-Allowed) lets it handle notification clicks
# that should focus/open pages anywhere on the site, not just /static/*.
@router.get("/sw.js")
async def push_service_worker():
    with open("static/sw.js", "r", encoding="utf-8") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


# ─── Customer Pages ───────────────────────────────────────────────────────

DEFAULT_OG_IMAGE = f"{SITE_URL}/static/images/favicon.svg"

HOME_TITLE = "clovical — Curated Fashion from Local Boutiques"
HOME_DESCRIPTION = (
    "clovical connects local boutique shops with online shoppers, offering "
    "curated, quality fashion for Boys & Girls, sourced from real "
    "local sellers."
)

@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    website_ld_json = _ld_json({
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "clovical",
        "url": f"{SITE_URL}/",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{SITE_URL}/products?search={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    })
    organization_ld_json = _ld_json({
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "clovical",
        "url": f"{SITE_URL}/",
        "logo": DEFAULT_OG_IMAGE,
    })
    return render(
        "customer/home.html",
        request,
        page_title=HOME_TITLE,
        page_description=HOME_DESCRIPTION,
        organization_ld_json=organization_ld_json,
        website_ld_json=website_ld_json,
    )

@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    gender = request.query_params.get("gender")
    title = "Girls Collection — clovical" if gender == "Girls" else (
        "Boys Collection — clovical" if gender == "Boys" else "Shop All Collections — clovical"
    )
    description = (
        "Browse clovical's full collection of curated kids' fashion from local "
        "boutique shops — filter by size, colour, category and price."
    )
    return render("customer/products.html", request, page_title=title, page_description=description)

@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: str):
    # Server-rendered fallback meta (used by crawlers/social bots that don't
    # run JS). The page's own JS still overwrites these once product data
    # loads client-side, so this is purely for SEO / link-preview purposes.
    # A plain, uncached select — deliberately NOT the /api/products/{id}
    # endpoint, so this never double-increments view_count.
    product_name = "Product"
    product_title = "Product — clovical"
    product_description = "Shop quality kids' clothing at clovical, connecting local boutique shops to online customers."
    product_image = DEFAULT_OG_IMAGE
    product_url = f"{SITE_URL}/product/{product_id}"
    product_ld_json = None
    breadcrumb_ld_json = None
    category = None

    try:
        res = await run_query(
            supabase_admin.table("products")
            .select("name,description,image,images,our_price,mrp,stock,category,gender")
            .eq("id", product_id)
            .single()
        )
        if res.data:
            data = res.data
            name = (data.get("name") or "Product").strip()
            desc = (data.get("description") or "").strip()
            if not desc:
                desc = f"Shop {name} at clovical — quality kids' clothing, delivered fast."
            ld_desc = desc  # full-length description for JSON-LD (no truncation needed there)
            if len(desc) > 160:
                desc = desc[:157].rstrip() + "..."

            product_name = name
            product_title = f"{name} — clovical"
            product_description = desc
            category = data.get("category")

            img = data.get("image")
            if img:
                product_image = img if img.startswith("http") else f"{SITE_URL}{img}"

            # Gather all image URLs (gallery) for JSON-LD, falling back to the
            # single `image` field for older products without an `images` array.
            raw_images = data.get("images") or []
            if not isinstance(raw_images, list) or not raw_images:
                raw_images = [img] if img else []
            ld_images = [
                (u if u.startswith("http") else f"{SITE_URL}{u}")
                for u in raw_images if u
            ] or [product_image]

            price = data.get("our_price")
            stock = data.get("stock") or 0

            product_ld_json = _ld_json({
                "@context": "https://schema.org",
                "@type": "Product",
                "name": name,
                "description": ld_desc,
                "image": ld_images,
                "url": product_url,
                "brand": {"@type": "Brand", "name": "clovical"},
                "offers": {
                    "@type": "Offer",
                    "url": product_url,
                    "priceCurrency": "INR",
                    "price": f"{float(price):.2f}" if price is not None else "0.00",
                    "availability": (
                        "https://schema.org/InStock" if stock > 0
                        else "https://schema.org/OutOfStock"
                    ),
                    "itemCondition": "https://schema.org/NewCondition",
                },
            })

            # BreadcrumbList mirrors the on-page breadcrumb: Home / Shop
            # (/ Category, if the product has one) / Product name.
            crumbs = [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": f"{SITE_URL}/"},
                {"@type": "ListItem", "position": 2, "name": "Shop", "item": f"{SITE_URL}/products"},
            ]
            position = 3
            if category:
                gender = data.get("gender") or "Girls"
                cat_qs = urlencode({"category": category, "gender": gender})
                crumbs.append({
                    "@type": "ListItem",
                    "position": position,
                    "name": category,
                    "item": f"{SITE_URL}/products?{cat_qs}",
                })
                position += 1
            crumbs.append({
                "@type": "ListItem", "position": position, "name": name, "item": product_url
            })
            breadcrumb_ld_json = _ld_json({
                "@context": "https://schema.org",
                "@type": "BreadcrumbList",
                "itemListElement": crumbs,
            })
    except Exception:
        # Product not found / DB hiccup — page still renders with generic
        # meta tags above, and the client-side fetch will show a proper
        # "not found" state to the user.
        pass

    return render(
        "customer/product_detail.html",
        request,
        product_id=product_id,
        product_name=product_name,
        product_title=product_title,
        product_description=product_description,
        product_image=product_image,
        product_url=product_url,
        product_category=category,
        product_ld_json=product_ld_json,
        breadcrumb_ld_json=breadcrumb_ld_json,
    )

@router.get("/cart", response_class=HTMLResponse)
async def cart(request: Request):
    return render("customer/cart.html", request)

@router.get("/wishlist", response_class=HTMLResponse)
async def wishlist(request: Request):
    return render("customer/wishlist.html", request)

@router.get("/checkout", response_class=HTMLResponse)
async def checkout(request: Request):
    return render("customer/checkout.html", request)

@router.get("/order/confirmation", response_class=HTMLResponse)
async def order_confirmation(request: Request):
    return render("customer/order_confirmation.html", request)

@router.get("/my-orders", response_class=HTMLResponse)
async def my_orders_page(request: Request):
    return render("customer/my_orders.html", request)

@router.get("/privacy-policy", response_class=HTMLResponse)
async def privacy_policy_page(request: Request):
    return render("customer/privacy_policy.html", request)

@router.get("/terms-and-conditions", response_class=HTMLResponse)
async def terms_and_conditions_page(request: Request):
    return render("customer/terms_and_conditions.html", request)

@router.get("/cookie-policy", response_class=HTMLResponse)
async def cookie_policy_page(request: Request):
    return render("customer/cookie_policy.html", request)


# ─── Admin Pages ──────────────────────────────────────────────────────────

@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if get_admin_from_request(request):
        return RedirectResponse("/admin/dashboard")
    return render("admin/login.html", request)

@router.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/dashboard.html", request)

@router.get("/admin/products", response_class=HTMLResponse)
async def admin_products(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/products.html", request)

@router.get("/admin/categories", response_class=HTMLResponse)
async def admin_categories(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/categories.html", request)

@router.get("/admin/shopkeepers", response_class=HTMLResponse)
async def admin_shopkeepers(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/shopkeepers.html", request)

@router.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/orders.html", request, nimbuspost_configured=nimbuspost_is_configured())

@router.get("/admin/analytics", response_class=HTMLResponse)
async def admin_analytics(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/analytics.html", request)


@router.get("/admin/requests", response_class=HTMLResponse)
async def admin_requests_page(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/requests.html", request)

@router.get("/admin/stock", response_class=HTMLResponse)
async def admin_stock_page(request: Request):
    if not get_admin_from_request(request):
        return RedirectResponse("/admin/login")
    return render("admin/stock.html", request)


# ─── Shopkeeper Panel Pages ─────────────────────────────────────────────────

@router.get("/shopkeeper/login", response_class=HTMLResponse)
async def shopkeeper_login_page(request: Request):
    if get_shopkeeper_from_request(request):
        return RedirectResponse("/shopkeeper/products")
    return render("shopkeeper/login.html", request)

@router.get("/shopkeeper/products", response_class=HTMLResponse)
async def shopkeeper_products_page(request: Request):
    if not get_shopkeeper_from_request(request):
        return RedirectResponse("/shopkeeper/login")
    return render("shopkeeper/products.html", request)

@router.get("/shopkeeper/orders", response_class=HTMLResponse)
async def shopkeeper_orders_page(request: Request):
    if not get_shopkeeper_from_request(request):
        return RedirectResponse("/shopkeeper/login")
    return render("shopkeeper/orders.html", request)

@router.get("/shopkeeper/stock", response_class=HTMLResponse)
async def shopkeeper_stock_page(request: Request):
    if not get_shopkeeper_from_request(request):
        return RedirectResponse("/shopkeeper/login")
    return render("shopkeeper/stock.html", request)

@router.get("/shopkeeper/analytics", response_class=HTMLResponse)
async def shopkeeper_analytics_page(request: Request):
    if not get_shopkeeper_from_request(request):
        return RedirectResponse("/shopkeeper/login")
    return render("shopkeeper/analytics.html", request)