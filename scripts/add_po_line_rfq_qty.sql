-- Add rfq_qty to purchase_order_line.
-- Run once against your PostgreSQL ERP/collab database if not already applied.

ALTER TABLE public.purchase_order_line
    ADD COLUMN IF NOT EXISTS rfq_qty INTEGER;
