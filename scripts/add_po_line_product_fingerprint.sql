-- Copy target: store vendor_items.product_fingerprint on purchase_order_line.
-- Run once against the ERP/collab PostgreSQL database.

ALTER TABLE public.purchase_order_line
    ADD COLUMN IF NOT EXISTS product_fingerprint JSONB;
