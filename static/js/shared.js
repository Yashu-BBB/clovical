// ═══════════════════════════════════════
// clovical — Shared JS (Premium Redesign)
// ═══════════════════════════════════════

// ─── SVG Icons ────────────────────────────────────────────────────────────
const Icons = {
  heart:    `<svg viewBox="0 0 24 24" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>`,
  heartFill:`<svg viewBox="0 0 24 24" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="fill:#0A0A0A;stroke:#0A0A0A"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>`,
  bag:      `<svg viewBox="0 0 24 24" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></svg>`,
  menu:     `<svg viewBox="0 0 24 24" stroke-width="1.5" stroke-linecap="round"><line x1="3" y1="7" x2="21" y2="7"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="17" x2="21" y2="17"/></svg>`,
  x:        `<svg viewBox="0 0 24 24" stroke-width="1.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
  arrow:    `<svg viewBox="0 0 24 24" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>`,
  arrowLeft:`<svg viewBox="0 0 24 24" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>`,
  check:    `<svg viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"><polyline points="20 6 9 17 4 12"/></svg>`,
  truck:    `<svg viewBox="0 0 24 24" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"><rect x="1" y="3" width="15" height="13"/><path d="M16 8h4l3 3v5h-7V8z"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/></svg>`,
};

// ─── Toast ─────────────────────────────────────────────────────────────────
function showToast(message, type = "success") {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => { toast.style.opacity = "0"; toast.style.transition = "opacity .4s"; }, 2600);
  setTimeout(() => toast.remove(), 3000);
}

// ─── API Helper ───────────────────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: "Request failed" }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return await res.json();
}

// ─── Cart ─────────────────────────────────────────────────────────────────
const Cart = {
  get() { return JSON.parse(localStorage.getItem("clovical_cart") || "[]"); },
  save(items) { localStorage.setItem("clovical_cart", JSON.stringify(items)); updateCartBadge(); },
  add(product, size, color) {
    const items = this.get();
    const existing = items.find(i => i.id === product.id && i.size === size && i.color === color);
    if (existing) {
      existing.qty = (existing.qty || 1) + 1;
    } else {
      items.push({ id: product.id, name: product.name, price: product.our_price, image: product.image, size, color, qty: 1 });
    }
    this.save(items);
    showToast("Added to cart");
  },
  remove(id, size, color) {
    const items = this.get().filter(i => !(i.id === id && i.size === size && i.color === color));
    this.save(items);
  },
  count() { return this.get().reduce((s, i) => s + (i.qty || 1), 0); },
  total() { return this.get().reduce((s, i) => s + i.price * (i.qty || 1), 0); },
  clear() { localStorage.removeItem("clovical_cart"); updateCartBadge(); }
};

// ─── Wishlist ─────────────────────────────────────────────────────────────
const Wishlist = {
  get() { return JSON.parse(localStorage.getItem("clovical_wishlist") || "[]"); },
  save(items) { localStorage.setItem("clovical_wishlist", JSON.stringify(items)); updateWishlistBadge(); },
  toggle(product) {
    const items = this.get();
    const idx = items.findIndex(i => i.id === product.id);
    if (idx > -1) {
      items.splice(idx, 1);
      showToast("Removed from wishlist", "warning");
    } else {
      items.push({ id: product.id, name: product.name, price: product.our_price, image: product.image, code: product.shopkeeper_code });
      showToast("Saved to wishlist");
    }
    this.save(items);
    return idx === -1;
  },
  has(id) { return this.get().some(i => i.id === id); },
  count() { return this.get().length; }
};

function updateCartBadge() {
  const badge = document.getElementById("cart-badge");
  if (badge) {
    const count = Cart.count();
    badge.textContent = count;
    badge.style.display = count > 0 ? "flex" : "none";
  }
}

function updateWishlistBadge() {
  const badge = document.getElementById("wishlist-badge");
  if (badge) {
    const count = Wishlist.count();
    badge.textContent = count;
    badge.style.display = count > 0 ? "flex" : "none";
  }
}

// ─── Skeleton ─────────────────────────────────────────────────────────────
function skeletonCards(container, count = 6) {
  container.innerHTML = Array(count).fill(`
    <div class="skeleton-card">
      <div class="skeleton skeleton-img"></div>
      <div style="padding:14px 16px">
        <div class="skeleton skeleton-text" style="width:65%;margin-bottom:8px"></div>
        <div class="skeleton skeleton-text" style="width:35%;height:11px"></div>
      </div>
    </div>
  `).join("");
}

// ─── Format ───────────────────────────────────────────────────────────────
function formatPrice(n) { return "₹" + Number(n).toLocaleString("en-IN"); }

// ─── Product Card ─────────────────────────────────────────────────────────
function renderProductCard(p) {
  const wishlisted = Wishlist.has(p.id);
  // Discount logic
  const hasDiscount = p.mrp && p.mrp > p.our_price;
  const discountPct = hasDiscount ? Math.round((1 - p.our_price / p.mrp) * 100) : 0;
  const priceHtml = hasDiscount
    ? `<div class="product-card-price">
        <span class="price-current">${formatPrice(p.our_price)}</span>
        <span class="price-mrp">${formatPrice(p.mrp)}</span>
        <span class="price-badge">-${discountPct}%</span>
       </div>`
    : `<div class="product-card-price">${formatPrice(p.our_price)}</div>`;

  return `
    <div class="product-card" onclick="window.location='/product/${p.id}'">
      <div class="product-img-wrap">
        <img class="product-img" src="${p.image || '/static/images/placeholder.svg'}" alt="${p.name}" loading="lazy">
        ${hasDiscount ? `<div class="discount-badge">-${discountPct}%</div>` : ''}
        <div class="product-card-overlay">
          <span class="btn btn-primary btn-sm" style="pointer-events:none">View Details</span>
        </div>
        <button class="wishlist-btn ${wishlisted ? 'active' : ''}"
          onclick="event.stopPropagation(); toggleWishlist(this, ${JSON.stringify(p).replace(/"/g,'&quot;')})"
          title="Save to wishlist">
          ${wishlisted ? Icons.heartFill : Icons.heart}
        </button>
      </div>
      <div class="product-card-body">
        ${p.category ? `<div class="product-card-category">${p.category}</div>` : ''}
        <div class="product-card-name">${p.name}</div>
        ${priceHtml}
      </div>
    </div>
  `;
}

function toggleWishlist(btn, product) {
  const added = Wishlist.toggle(product);
  btn.innerHTML = added ? Icons.heartFill : Icons.heart;
  btn.classList.toggle("active", added);
}

// ─── Mobile Nav ───────────────────────────────────────────────────────────
function toggleMobileMenu() {
  const nav = document.getElementById("mobile-nav");
  const hamBtn = document.getElementById("hamburger-btn");
  if (!nav) return;
  const isOpen = nav.classList.toggle("open");
  if (hamBtn) hamBtn.innerHTML = isOpen ? Icons.x : Icons.menu;
}

// ─── Init ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  updateCartBadge();
  updateWishlistBadge();
});