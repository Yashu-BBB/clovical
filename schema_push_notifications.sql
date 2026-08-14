-- ═══════════════════════════════════════════════════════════════════════
-- clovical — Push Notifications (FCM) migration
-- Run in Supabase SQL Editor. Safe to re-run (IF NOT EXISTS throughout).
--
-- Stores one row per registered device/browser. Same recipient_type /
-- recipient_id convention as the `notifications` table:
--   • admin      → recipient_id is NULL (any admin-panel device)
--   • shopkeeper → recipient_id = shopkeepers.id (as text)
--   • customer   → recipient_id = customers.id   (as text, uuid)
--
-- fcm_token is UNIQUE and is the upsert key: re-registering the same
-- browser/device just refreshes ownership + timestamp. This also means a
-- shared device that logs out and logs back in as a different
-- user/shopkeeper/admin automatically stops delivering push to whoever
-- registered that token previously.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('admin', 'shopkeeper', 'customer')),
    recipient_id   TEXT,                 -- NULL for admin; shopkeeper_id / customer_id otherwise
    fcm_token      TEXT NOT NULL UNIQUE,
    user_agent     TEXT,                 -- optional, helpful for debugging which device a token belongs to
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast "give me every device for this recipient" lookup (used on every
-- notification event to fan out the push).
CREATE INDEX IF NOT EXISTS idx_push_subscriptions_recipient
    ON push_subscriptions (recipient_type, recipient_id);

-- RLS: all reads/writes go through the service-role key (backend only),
-- same convention as every other table in this app.
ALTER TABLE push_subscriptions ENABLE ROW LEVEL SECURITY;