-- Performance index for sales trend / ADC aggregation by product and date.
CREATE INDEX IF NOT EXISTS idx_sales_transaction_product_date
    ON sales_transactions (product_id, transaction_date);
