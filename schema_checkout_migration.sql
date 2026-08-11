-- ═══════════════════════════════════════════════════════════════════
-- clovical SCHEMA MIGRATION — Checkout v2
-- Adds: customers, payment_records, orders.user_id,
--       orders.checkout_group_id, extended CHECK constraints
--
-- Run in Supabase SQL Editor.
-- Safe to re-run: all statements are additive / IF NOT EXISTS / DO blocks.
-- ═══════════════════════════════════════════════════════════════════

-- ─── CUSTOMERS ──────────────────────────────────────────────────────
-- Stores every shopper who logs in at checkout (Google OAuth or mobile OTP).
-- A customer created via OTP has phone set; via Google has google_id set.
-- If they later use both methods with the same number/email, the record
-- is merged so they never end up with two accounts.
CREATE TABLE IF NOT EXISTS customers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone         TEXT,            -- 10-digit verified mobile, no +91 prefix
    email         TEXT,            -- from Google OAuth; NULL for OTP-only users
    name          TEXT,            -- from Google profile or provided at checkout
    google_id     TEXT,            -- Google's immutable "sub" claim
    auth_provider TEXT NOT NULL DEFAULT 'phone'
                  CHECK (auth_provider IN ('google', 'phone', 'both')),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Partial unique indexes: NULL values are not compared so a customer
-- without a phone can coexist with others who also have no phone yet.
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_phone
    ON customers (phone) WHERE phone IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_customers_google_id
    ON customers (google_id) WHERE google_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customers_email
    ON customers (email);

-- RLS: All reads/writes go through the service-role key (backend only).
-- No anon/authenticated policies — customers never touch Supabase directly.
ALTER TABLE customers ENABLE ROW LEVEL SECURITY;


-- ─── PAYMENT RECORDS ────────────────────────────────────────────────
-- Cashfree payment audit trail. One row per Cashfree payment attempt.
-- An order group may have multiple rows if the customer retries payment.
-- cashfree_order_id is unique so duplicate webhooks can be detected.
CREATE TABLE IF NOT EXISTS payment_records (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    checkout_group_id    UUID NOT NULL,     -- joins to orders.checkout_group_id
    customer_id          UUID REFERENCES customers(id) ON DELETE SET NULL,
    cashfree_order_id    TEXT NOT NULL,     -- e.g. "clv_<32-hex-chars>"
    cashfree_payment_id  TEXT,             -- cf_payment_id from webhook
    amount               NUMERIC(10,2) NOT NULL,
    currency             TEXT NOT NULL DEFAULT 'INR',
    payment_status       TEXT NOT NULL DEFAULT 'PENDING',
    -- Cashfree statuses: PENDING | SUCCESS | FAILED | CANCELLED
    --                    USER_DROPPED | VOID | NOT_ATTEMPTED
    payment_method       TEXT,             -- 'upi', 'card', 'netbanking', etc.
    gateway_response     JSONB,            -- full webhook payload for audit
    webhook_received_at  TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_cashfree_order_id UNIQUE (cashfree_order_id)
);

CREATE INDEX IF NOT EXISTS idx_payment_records_group_id
    ON payment_records (checkout_group_id);
CREATE INDEX IF NOT EXISTS idx_payment_records_customer_id
    ON payment_records (customer_id);
CREATE INDEX IF NOT EXISTS idx_payment_records_cf_order_id
    ON payment_records (cashfree_order_id);
CREATE INDEX IF NOT EXISTS idx_payment_records_status
    ON payment_records (payment_status);

ALTER TABLE payment_records ENABLE ROW LEVEL SECURITY;


-- ─── ORDERS: new columns ─────────────────────────────────────────────
-- user_id links a website-checkout order to the logged-in customer.
-- Orders placed via the old WhatsApp flow have user_id = NULL — that's fine.
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES customers(id) ON DELETE SET NULL;

-- checkout_group_id groups all order rows that belong to a single cart checkout.
-- Single-item checkouts: one order row, one group UUID.
-- Multi-item checkouts: N order rows, same group UUID across all of them.
-- COD and Cashfree both use this.
ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS checkout_group_id UUID;

CREATE INDEX IF NOT EXISTS idx_orders_user_id
    ON orders (user_id);
CREATE INDEX IF NOT EXISTS idx_orders_checkout_group_id
    ON orders (checkout_group_id);


-- ─── Extend payment_type CHECK ───────────────────────────────────────
-- Old constraint only allowed 'upi' and 'cod'.
-- New constraint adds 'cashfree' for website-native online payments.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'orders_payment_type_check'
          AND table_name      = 'orders'
          AND table_schema    = 'public'
    ) THEN
        ALTER TABLE orders DROP CONSTRAINT orders_payment_type_check;
    END IF;
END $$;

ALTER TABLE orders
    ADD CONSTRAINT orders_payment_type_check
    CHECK (payment_type IN ('upi', 'cod', 'cashfree'));


-- ─── Extend payment_status CHECK ─────────────────────────────────────
-- Old constraint: pending | received | verified | failed
-- New: adds 'awaiting_payment' — order created in DB but Cashfree payment
-- not yet initiated/completed. Distinct from 'pending' (which means COD,
-- waiting for admin confirmation) so admins can filter correctly.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'orders_payment_status_check'
          AND table_name      = 'orders'
          AND table_schema    = 'public'
    ) THEN
        ALTER TABLE orders DROP CONSTRAINT orders_payment_status_check;
    END IF;
END $$;

ALTER TABLE orders
    ADD CONSTRAINT orders_payment_status_check
    CHECK (payment_status IN (
        'pending',           -- COD: waiting for admin confirmation
        'awaiting_payment',  -- Cashfree: order created, payment not yet complete
        'received',          -- legacy: WhatsApp UPI screenshot received
        'verified',          -- payment confirmed (Cashfree webhook SUCCESS)
        'failed'             -- payment failed / cancelled
    ));


-- ─── SETTINGS: migration marker ──────────────────────────────────────
INSERT INTO settings (key, value)
VALUES ('checkout_v2_enabled', 'true')
ON CONFLICT (key) DO NOTHING;
