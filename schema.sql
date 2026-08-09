-- ═══════════════════════════════════════════════════
-- clovical DATABASE SCHEMA
-- Run in Supabase SQL Editor
-- ═══════════════════════════════════════════════════

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── SHOPKEEPERS ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shopkeepers (
    id SERIAL PRIMARY KEY,
    shop_name TEXT NOT NULL,
    shopkeeper_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── PRODUCTS ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    our_price NUMERIC(10,2) NOT NULL,
    shopkeeper_price NUMERIC(10,2) NOT NULL,
    sizes JSONB DEFAULT '[]',
    colors JSONB DEFAULT '[]',
    image TEXT,
    category TEXT,
    featured BOOLEAN DEFAULT FALSE,
    stock INTEGER DEFAULT 1,
    shopkeeper_id INTEGER REFERENCES shopkeepers(id) ON DELETE SET NULL,
    shopkeeper_code TEXT,
    view_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── ORDERS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    customer_address TEXT NOT NULL,
    customer_city TEXT NOT NULL,
    product_id UUID,
    product_name TEXT NOT NULL,
    size TEXT NOT NULL,
    color TEXT NOT NULL,
    our_price NUMERIC(10,2) NOT NULL,
    shopkeeper_price NUMERIC(10,2) NOT NULL,
    profit NUMERIC(10,2) GENERATED ALWAYS AS (our_price - shopkeeper_price) STORED,
    shopkeeper_id INTEGER,
    shopkeeper_code TEXT,
    payment_type TEXT DEFAULT 'upi' CHECK (payment_type IN ('upi','cod')),
    payment_status TEXT DEFAULT 'pending' CHECK (payment_status IN ('pending','received','verified','failed')),
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','shipped','delivered','cancelled','refunded')),
    tracking_id TEXT,
    courier_name TEXT,
    review_rating INTEGER CHECK (review_rating BETWEEN 1 AND 5),
    refund_status TEXT DEFAULT 'none' CHECK (refund_status IN ('none','pending','processed')),
    agent_state JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── ADMINS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── VISITORS ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS visitors (
    id SERIAL PRIMARY KEY,
    page TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── REVIEWS ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
    customer_phone TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── ROW LEVEL SECURITY ───────────────────────────────────────────────────
ALTER TABLE shopkeepers ENABLE ROW LEVEL SECURITY;
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE admins ENABLE ROW LEVEL SECURITY;
ALTER TABLE visitors ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;

-- Products: public can only read (without shopkeeper_price)
-- NOTE: shopkeeper_price is excluded at API level, not RLS level
-- All writes go through service role key only.

-- Public read on products
CREATE POLICY "public_read_products"
    ON products FOR SELECT
    USING (true);

-- No public write
-- (All other operations require service role)

-- ─── INDEXES ──────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_products_featured ON products(featured);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_orders_customer_phone ON orders(customer_phone);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_visitors_created_at ON visitors(created_at);

-- ─── STORAGE BUCKET ───────────────────────────────────────────────────────
-- Run via Supabase Dashboard → Storage → New Bucket
-- Name: product-images
-- Public: true
-- (Cannot be created via SQL, do it in Supabase dashboard)

-- ─── SAMPLE ADMIN (change password!) ─────────────────────────────────────
-- Password: admin123 (bcrypt hash — change via admin panel after setup)
INSERT INTO admins (username, password)
VALUES ('admin', '$2b$12$KIX0.GGqK4R2WPNiPdFJHO0sMQ8WnmEjRaJ.8jN5tPSaP.aA8bHRS')
ON CONFLICT (username) DO NOTHING;
-- ═══════════════════════════════════════════════════
-- clovical SCHEMA UPDATE — Categories + Gender
-- Run in Supabase SQL Editor
-- ═══════════════════════════════════════════════════

-- Add gender column to products
ALTER TABLE products ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT 'Girls' CHECK (gender IN ('Boys', 'Girls'));

-- Create categories table
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    icon TEXT NOT NULL DEFAULT '🏷️',
    gender TEXT NOT NULL CHECK (gender IN ('Boys', 'Girls')),
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;

-- Public can read categories
CREATE POLICY "public_read_categories" ON categories FOR SELECT USING (true);

-- Insert default categories
INSERT INTO categories (name, icon, gender, sort_order) VALUES
-- Girls
('Kurti', '👘', 'Girls', 1),
('Saree', '🥻', 'Girls', 2),
('Lehenga', '✨', 'Girls', 3),
('Anarkali', '🌸', 'Girls', 4),
('Dress', '👗', 'Girls', 5),
('Top', '👚', 'Girls', 6),
('Maxi', '💃', 'Girls', 7),
('Co-ord Set', '🎀', 'Girls', 8),
('Gown', '👸', 'Girls', 9),
('Ethnic Wear', '🪷', 'Girls', 10),
-- Boys
('T-Shirt', '👕', 'Boys', 1),
('Shirt', '👔', 'Boys', 2),
('Jeans', '👖', 'Boys', 3),
('Track Pants', '🏃', 'Boys', 4),
('Shorts', '🩳', 'Boys', 5),
('Ethnic Kurta', '🧥', 'Boys', 6),
('Hoodie', '🧣', 'Boys', 7),
('Formal Wear', '💼', 'Boys', 8)
ON CONFLICT (name) DO NOTHING;

-- Index for fast gender filtering
CREATE INDEX IF NOT EXISTS idx_products_gender ON products(gender);
CREATE INDEX IF NOT EXISTS idx_categories_gender ON categories(gender);

-- ═══════════════════════════════════════════════════
-- clovical DATABASE SCHEMA
-- Run in Supabase SQL Editor
-- ═══════════════════════════════════════════════════

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── SHOPKEEPERS ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS shopkeepers (
    id SERIAL PRIMARY KEY,
    shop_name TEXT NOT NULL,
    shopkeeper_name TEXT NOT NULL,
    contact TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── PRODUCTS ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    our_price NUMERIC(10,2) NOT NULL,
    shopkeeper_price NUMERIC(10,2) NOT NULL,
    sizes JSONB DEFAULT '[]',
    colors JSONB DEFAULT '[]',
    image TEXT,
    category TEXT,
    featured BOOLEAN DEFAULT FALSE,
    stock INTEGER DEFAULT 1,
    -- Per-variant stock counts, e.g. {"S": 4, "M": 0, "L": 2} / {"Red": 3, "Black": 0}.
    -- A size/color that is absent from the map has no per-variant restriction
    -- (falls back to the overall `stock` count) — this keeps existing products
    -- that were never given variant-level stock working exactly as before.
    size_stock JSONB DEFAULT '{}',
    color_stock JSONB DEFAULT '{}',
    shopkeeper_id INTEGER REFERENCES shopkeepers(id) ON DELETE SET NULL,
    shopkeeper_code TEXT,
    view_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── ORDERS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_name TEXT NOT NULL,
    customer_phone TEXT NOT NULL,
    customer_address TEXT NOT NULL,
    customer_city TEXT NOT NULL,
    product_id UUID,
    product_name TEXT NOT NULL,
    size TEXT NOT NULL,
    color TEXT NOT NULL,
    our_price NUMERIC(10,2) NOT NULL,
    shopkeeper_price NUMERIC(10,2) NOT NULL,
    profit NUMERIC(10,2) GENERATED ALWAYS AS (our_price - shopkeeper_price) STORED,
    shopkeeper_id INTEGER,
    shopkeeper_code TEXT,
    payment_type TEXT DEFAULT 'upi' CHECK (payment_type IN ('upi','cod')),
    payment_status TEXT DEFAULT 'pending' CHECK (payment_status IN ('pending','received','verified','failed')),
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending','confirmed','shipped','delivered','cancelled','refunded')),
    tracking_id TEXT,
    courier_name TEXT,
    review_rating INTEGER CHECK (review_rating BETWEEN 1 AND 5),
    refund_status TEXT DEFAULT 'none' CHECK (refund_status IN ('none','pending','processed')),
    agent_state JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── ADMINS ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS admins (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── VISITORS ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS visitors (
    id SERIAL PRIMARY KEY,
    page TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── REVIEWS ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
    id SERIAL PRIMARY KEY,
    order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
    customer_phone TEXT NOT NULL,
    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ─── ROW LEVEL SECURITY ───────────────────────────────────────────────────
ALTER TABLE shopkeepers ENABLE ROW LEVEL SECURITY;
ALTER TABLE products ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE admins ENABLE ROW LEVEL SECURITY;
ALTER TABLE visitors ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;

-- Products: public can only read (without shopkeeper_price)
-- NOTE: shopkeeper_price is excluded at API level, not RLS level
-- All writes go through service role key only.

-- Public read on products
CREATE POLICY "public_read_products"
    ON products FOR SELECT
    USING (true);

-- No public write
-- (All other operations require service role)

-- ─── INDEXES ──────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_products_featured ON products(featured);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_orders_customer_phone ON orders(customer_phone);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_visitors_created_at ON visitors(created_at);

-- ─── STORAGE BUCKET ───────────────────────────────────────────────────────
-- Run via Supabase Dashboard → Storage → New Bucket
-- Name: product-images
-- Public: true
-- (Cannot be created via SQL, do it in Supabase dashboard)

-- ─── SAMPLE ADMIN (change password!) ─────────────────────────────────────
-- Password: admin123 (bcrypt hash — change via admin panel after setup)
INSERT INTO admins (username, password)
VALUES ('admin', '$2b$12$KIX0.GGqK4R2WPNiPdFJHO0sMQ8WnmEjRaJ.8jN5tPSaP.aA8bHRS')
ON CONFLICT (username) DO NOTHING;
-- ═══════════════════════════════════════════════════
-- clovical SCHEMA UPDATE — Categories + Gender
-- Run in Supabase SQL Editor
-- ═══════════════════════════════════════════════════

-- Add gender column to products
ALTER TABLE products ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT 'Girls' CHECK (gender IN ('Boys', 'Girls'));

-- Create categories table
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    icon TEXT NOT NULL DEFAULT '🏷️',
    gender TEXT NOT NULL CHECK (gender IN ('Boys', 'Girls')),
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Enable RLS
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;

-- Public can read categories
CREATE POLICY "public_read_categories" ON categories FOR SELECT USING (true);

-- Insert default categories
INSERT INTO categories (name, icon, gender, sort_order) VALUES
-- Girls
('Kurti', '👘', 'Girls', 1),
('Saree', '🥻', 'Girls', 2),
('Lehenga', '✨', 'Girls', 3),
('Anarkali', '🌸', 'Girls', 4),
('Dress', '👗', 'Girls', 5),
('Top', '👚', 'Girls', 6),
('Maxi', '💃', 'Girls', 7),
('Co-ord Set', '🎀', 'Girls', 8),
('Gown', '👸', 'Girls', 9),
('Ethnic Wear', '🪷', 'Girls', 10),
-- Boys
('T-Shirt', '👕', 'Boys', 1),
('Shirt', '👔', 'Boys', 2),
('Jeans', '👖', 'Boys', 3),
('Track Pants', '🏃', 'Boys', 4),
('Shorts', '🩳', 'Boys', 5),
('Ethnic Kurta', '🧥', 'Boys', 6),
('Hoodie', '🧣', 'Boys', 7),
('Formal Wear', '💼', 'Boys', 8)
ON CONFLICT (name) DO NOTHING;

-- Index for fast gender filtering
CREATE INDEX IF NOT EXISTS idx_products_gender ON products(gender);
CREATE INDEX IF NOT EXISTS idx_categories_gender ON categories(gender);