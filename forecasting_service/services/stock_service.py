from sqlalchemy.orm import Session
from typing import Optional
import logging
import requests
from decimal import Decimal
from config import settings

logger = logging.getLogger(__name__)

class StockService:

    @staticmethod
    def get_stock_batches(db: Session, product_id: int) -> list:
        """
        Get all stock batch entries for a product from NestJS store-batch-stock API.
        Calls: GET /store-batch-stock?itemId={product_id}
        Returns list of all stock batch entries with full details.
        """

        print("IN GET STOCK BATCHES")
        if not settings.nestjs_api_url:
            logger.warning("NestJS API URL not configured. Returning empty list.")
            return []
        
        try:
            api_url = f"{settings.nestjs_api_url}/store-batch-stock"
            print("API URL: ",api_url)
            
            # Fetch all pages of stock entries
            all_stock_items = []
            page = 1
            limit = 100
            
            while True:
                params = {
                    "itemId": product_id,
                    "limit": limit,
                    "page": page
                }
                
                logger.debug(f"Fetching stock batches from NestJS API: {api_url} for product_id {product_id}, page {page}")
                response = requests.get(api_url, params=params, timeout=10)
                
                if response.status_code != 200:
                    logger.warning(f"NestJS API returned status {response.status_code} for product_id {product_id}.")
                    return []
                
                data = response.json()
                stock_items = data.get("items", [])
                all_stock_items.extend(stock_items)
                
                # Check if there are more pages
                next_page = data.get("nextPage")
                if not next_page or not stock_items:
                    break
                
                page = next_page
            
            return all_stock_items
                
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error calling NestJS API for product_id {product_id}: {str(e)}.")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error fetching stock batches for product_id {product_id}: {str(e)}.")
            return []

    @staticmethod
    def get_current_stock(db: Session, product_id: int) -> int:
        """
        Fetch current stock balance for product from NestJS store-batch-stock API.
        Uses get_stock_batches() and sums up available_qty across all batches.
        Returns sum of available_qty (generated column: onHandQty - reservedQty - committedQty).
        """
        logger.info(f"📦 [STOCK] Fetching stock for product_id: {product_id}")
        all_stock_items = StockService.get_stock_batches(db, product_id)
        
        if not all_stock_items:
            logger.info(f"📦 [STOCK] No stock entries found for product_id {product_id} - returning 0")
            return 0
        
        logger.info(f"📦 [STOCK] Found {len(all_stock_items)} stock batch(es) for product_id {product_id}")
        
        # Helper function to safely convert to Decimal
        def safe_decimal(value, default=0):
            """Safely convert value to Decimal, handling None/null/empty strings"""
            if value is None:
                return Decimal(default)
            if isinstance(value, (int, float)):
                return Decimal(str(value))
            if isinstance(value, str):
                value = value.strip()
                if value == "" or value.lower() in ("null", "none", "n/a", "na"):
                    return Decimal(default)
                try:
                    return Decimal(value)
                except (ValueError, TypeError) as e:
                    logger.warning(f"Invalid decimal value '{value}': {str(e)}, using default: {default}")
                    return Decimal(default)
            try:
                return Decimal(str(value))
            except (ValueError, TypeError) as e:
                logger.warning(f"Invalid decimal value {value} (type: {type(value).__name__}): {str(e)}, using default: {default}")
                return Decimal(default)
        
        # Calculate total available stock
        # Use availableQty (generated column) if available, otherwise calculate it
        total_available = Decimal(0)
        for item in all_stock_items:
            try:
                # Try to use availableQty first (generated column from DB: on_hand_qty - reserved_qty - committed_qty)
                if "availableQty" in item and item.get("availableQty") is not None:
                    available = safe_decimal(item.get("availableQty"), 0)
                else:
                    # Fallback: Calculate available_qty: onHandQty - reservedQty - committedQty
                    on_hand = safe_decimal(item.get("onHandQty"), 0)
                    reserved = safe_decimal(item.get("reservedQty"), 0)
                    committed = safe_decimal(item.get("committedQty"), 0)
                    available = on_hand - reserved - committed
                
                if available > 0:
                    total_available += available
            except Exception as e:
                logger.warning(f"Error processing stock item for product_id {product_id}: {str(e)}. Skipping this item.", exc_info=True)
                continue
        
        # Convert to int, return 0 if negative
        stock_qty = int(total_available) if total_available > 0 else 0
        
        logger.info(f"📦 [STOCK] Total available stock for product_id {product_id}: {stock_qty} (from {len(all_stock_items)} batches, total_available={total_available})")
        return stock_qty
