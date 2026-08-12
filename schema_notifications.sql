-- ═══════════════════════════════════════════════════════════════════════
-- clovical — Notification System migration
-- Run in Supabase SQL Editor. Safe to re-run (IF NOT EXISTS throughout).
--
-- One shared table serves all three recipient types:
--   • admin      → recipient_id is NULL (broadcast to anyone with an admin
--                  session — there's a single shared admin login today)
--   • shopkeeper → recipient_id = shopkeepers.id (as text)
--   • customer   → recipient_id = customers.id   (as text, uuid)
--
-- Events covered:
--   admin:      new_order, new_request, out_of_stock
--   shopkeeper: product_ordered, request_accepted, request_rejected
--   customer:   order_created, order_confirmed, order_shipped, order_delivered
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS notifications (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_type TEXT NOT NULL CHECK (recipient_type IN ('admin', 'shopkeeper', 'customer')),
    recipient_id   TEXT,                 -- NULL for admin (broadcast); shopkeeper_id / customer_id otherwise
    type           TEXT NOT NULL,        -- e.g. 'new_order', 'out_of_stock', 'request_accepted', 'order_shipped'
    title          TEXT NOT NULL,
    message        TEXT,
    link           TEXT,                 -- frontend path the notification should open, e.g. '/admin/orders'
    order_id       UUID REFERENCES orders(id) ON DELETE SET NULL,
    request_id     UUID REFERENCES product_requests(id) ON DELETE SET NULL,
    product_id     UUID REFERENCES products(id) ON DELETE SET NULL,
    is_read        BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast "give me this recipient's unread/recent notifications" lookups.
CREATE INDEX IF NOT EXISTS idx_notifications_recipient
    ON notifications (recipient_type, recipient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_unread
    ON notifications (recipient_type, recipient_id, is_read)
    WHERE is_read = FALSE;

-- RLS: all reads/writes go through the service-role key (backend only),
-- same convention as every other table in this app.
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;

-- Optional housekeeping: notifications older than 90 days are safe to purge
-- periodically (e.g. via a scheduled Supabase function or cron job) —
-- nothing in the app depends on old rows existing. Not required to run now.
-- DELETE FROM notifications WHERE created_at < NOW() - INTERVAL '90 days';