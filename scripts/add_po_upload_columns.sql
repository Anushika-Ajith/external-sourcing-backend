-- Add PO upload / revision columns to live purchase_order_header table.
-- Run once against your PostgreSQL ERP/collab database.

ALTER TABLE public.purchase_order_header
    ADD COLUMN IF NOT EXISTS document_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS business_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS revision_no INTEGER DEFAULT 1,
    ADD COLUMN IF NOT EXISTS current_revision BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS attachment_path VARCHAR(500);

CREATE INDEX IF NOT EXISTS idx_purchase_order_header_document_hash
    ON public.purchase_order_header (document_hash);
