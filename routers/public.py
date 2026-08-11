from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from utils.auth_utils import get_admin_from_request, get_shopkeeper_from_request
from utils.nimbuspost import is_configured as nimbuspost_is_configured

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def render(template: str, request: Request, **ctx):
    return templates.TemplateResponse(template, {"request": request, **ctx})


# ─── Customer Pages ───────────────────────────────────────────────────────

@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    return render("customer/home.html", request)

@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    return render("customer/products.html", request)

@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(request: Request, product_id: str):
    return render("customer/product_detail.html", request, product_id=product_id)

@router.get("/cart", response_class=HTMLResponse)
async def cart(request: Request):
    return render("customer/cart.html", request)

@router.get("/wishlist", response_class=HTMLResponse)
async def wishlist(request: Request):
    return render("customer/wishlist.html", request)

@router.get("/checkout", response_class=HTMLResponse)
async def checkout(request: Request):
    return render("customer/checkout.html", request)


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