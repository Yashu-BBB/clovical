from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from utils.auth_utils import get_admin_from_request, get_shopkeeper_from_request
from utils.nimbuspost import is_configured as nimbuspost_is_configured
from utils.db import supabase_admin, run_query

router = APIRouter()
templates = Jinja2Templates(directory="templates")

SITE_URL = "https://clovical.in"


def render(template: str, request: Request, **ctx):
    return templates.TemplateResponse(template, {"request": request, **ctx})


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

    all_urls = static_urls + product_urls

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


# ─── Customer Pages ───────────────────────────────────────────────────────

@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    return render("customer/home.html", request)

@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    return render("customer/products.html", request)

DEFAULT_OG_IMAGE = f"{SITE_URL}/static/images/favicon.svg"

@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: str):
    # Server-rendered fallback meta (used by crawlers/social bots that don't
    # run JS). The page's own JS still overwrites these once product data
    # loads client-side, so this is purely for SEO / link-preview purposes.
    # A plain, uncached select — deliberately NOT the /api/products/{id}
    # endpoint, so this never double-increments view_count.
    product_title = "Product — clovical"
    product_description = "Shop quality kids' clothing at clovical, connecting local boutique shops to online customers."
    product_image = DEFAULT_OG_IMAGE

    try:
        res = await run_query(
            supabase_admin.table("products")
            .select("name,description,image")
            .eq("id", product_id)
            .single()
        )
        if res.data:
            name = (res.data.get("name") or "Product").strip()
            desc = (res.data.get("description") or "").strip()
            if not desc:
                desc = f"Shop {name} at clovical — quality kids' clothing, delivered fast."
            if len(desc) > 160:
                desc = desc[:157].rstrip() + "..."

            product_title = f"{name} — clovical"
            product_description = desc

            img = res.data.get("image")
            if img:
                product_image = img if img.startswith("http") else f"{SITE_URL}{img}"
    except Exception:
        # Product not found / DB hiccup — page still renders with generic
        # meta tags above, and the client-side fetch will show a proper
        # "not found" state to the user.
        pass

    return render(
        "customer/product_detail.html",
        request,
        product_id=product_id,
        product_title=product_title,
        product_description=product_description,
        product_image=product_image,
        product_url=f"{SITE_URL}/product/{product_id}",
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
    return render("customer/cookies_policy.html", request)


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