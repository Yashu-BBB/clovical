-- ═══════════════════════════════════════════════════════════════════
-- clovical SCHEMA MIGRATION — Order details / cancel / reviews
-- Run in Supabase SQL Editor. Safe to re-run (IF NOT EXISTS / DO blocks).
--
-- Nothing needed on `orders` itself: status already allows 'cancelled'
-- and refund_status already allows 'pending' (see schema.sql /
-- schema_checkout_migration.sql), which is what the new cancel-order
-- endpoint uses — no new columns required there.
-- ═══════════════════════════════════════════════════════════════════

-- ─── REVIEWS: add text + product linkage ─────────────────────────────
-- The existing `reviews` table only stored a rating tied to an order.
-- Adding: review_text (the actual written review), product_id
-- (denormalized from the order at write time so the product page can
-- query reviews directly without joining through orders — same
-- denormalization pattern already used elsewhere, e.g. orders.product_image),
-- and customer_name (for display, so the product page never has to expose
-- customer_phone).
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS review_text TEXT;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS product_id UUID REFERENCES products(id) ON DELETE CASCADE;
ALTER TABLE reviews ADD COLUMN IF NOT EXISTS customer_name TEXT;

-- One review per order — the customer submits a single rating/write-up for
-- what they bought, not repeated reviews on the same order.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_reviews_order_id'
    ) THEN
        ALTER TABLE reviews ADD CONSTRAINT uq_reviews_order_id UNIQUE (order_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_reviews_product_id ON reviews (product_id);