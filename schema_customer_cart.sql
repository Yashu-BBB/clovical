-- ═══════════════════════════════════════════════════════════════════════
-- clovical — Customer cart/wishlist persistence migration
-- Run in Supabase SQL Editor. Safe to re-run (IF NOT EXISTS throughout).
--
-- Previously cart/wishlist lived only in the browser's localStorage, even
-- for logged-in customers — cleared browser data or switching devices
-- meant losing it. One row per customer here, synced from the frontend
-- (see static/js/shared.js's CartSync) on login and on every change while
-- logged in. Guests still use localStorage only, same as before; nothing
-- changes for them.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS customer_carts (
    customer_id UUID PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
    cart        JSONB NOT NULL DEFAULT '[]'::jsonb,   -- array of {id,name,price,image,size,color,qty}
    wishlist    JSONB NOT NULL DEFAULT '[]'::jsonb,   -- array of {id,name,price,image,code}
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS: all reads/writes go through the service-role key (backend only),
-- same convention as every other table in this app.
ALTER TABLE customer_carts ENABLE ROW LEVEL SECURITY;