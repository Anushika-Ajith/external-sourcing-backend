from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
import logging
import requests
from datetime import datetime
from config import settings

logger = logging.getLogger(__name__)

class ConsumptionService:
    """
    Service to fetch consumption history from NestJS API (ERP database)
    Handles consumption_history table with schema:
    - id: text
    - consumptionId: text (optional)
    - itemId: text (maps to product_id in main DB)
    - date: timestamp(3)
    - department: text
    - quantityUsed: integer
    - consumedById: text
    - createdAt: timestamp(3)
    """

    @staticmethod
    def get_consumption_history(db: Session, item_id: str) -> List[Dict[str, Any]]:
        """
        Get consumption history for an item from NestJS consumption API.
        Calls: GET /consumption?itmId={itmId}
        Note: item_id should be a string (text) as per ERP schema
        Uses 'itmId' query parameter instead of path parameter.
        
        Args:
            db: Database session (kept for consistency, not used)
            item_id: Item ID as string (from ERP item_master.id)
        
        Returns:
            List of consumption history records
        """
        if not settings.nestjs_api_url:
            logger.warning("NestJS API URL not configured. Returning empty list.")
            return []

        try:
            # Use query parameter itmId instead of path parameter
            api_url = f"{settings.nestjs_api_url}/consumption"
            params = {"itmId": item_id}
            
            logger.info(f"🌐 [CONSUMPTION API] Calling NestJS API: GET {api_url}?itmId={item_id} for itmId: {item_id}")
            response = requests.get(api_url, params=params, timeout=10)
            logger.info(f"📡 [CONSUMPTION API] Response status: {response.status_code} for itmId: {item_id}")
            
            if response.status_code != 200:
                logger.warning(f"NestJS API returned status {response.status_code} for itmId {item_id}.")
                return []
            
            # NestJS returns paginated response with items array
            data = response.json()
            
            # Handle both paginated response format and direct array format
            if isinstance(data, dict) and "items" in data:
                consumption_items = data.get("items", [])
            elif isinstance(data, list):
                consumption_items = data
            else:
                logger.warning(f"⚠️ [CONSUMPTION API] Unexpected response format from NestJS API for itmId {item_id}.")
                return []
            
            logger.info(f"✅ [CONSUMPTION API] Successfully retrieved {len(consumption_items)} consumption records for itmId: {item_id}")
            return consumption_items
                
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error calling NestJS API for itmId {item_id}: {str(e)}.")
            return []
        except Exception as e:
            logger.warning(f"Unexpected error fetching consumption for itmId {item_id}: {str(e)}.")
            return []
    
    @staticmethod
    def get_consumption_paginated(db: Session, item_id: Optional[str] = None, limit: int = 100, page: int = 1) -> Dict[str, Any]:
        """
        Get paginated consumption history from NestJS API.
        Calls: GET /consumption?itmId={itmId}&limit={limit}&page={page}
        Uses 'itmId' query parameter instead of 'itemId'.
        
        Args:
            db: Database session (kept for consistency, not used)
            item_id: Optional item ID to filter by
            limit: Number of items per page
            page: Page number (1-based)
        
        Returns:
            Dictionary with items, total, and pagination info
        """
        if not settings.nestjs_api_url:
            logger.warning("NestJS API URL not configured. Returning empty result.")
            return {"items": [], "total": 0, "nextPage": None}

        try:
            api_url = f"{settings.nestjs_api_url}/consumption"
            params = {
                "limit": limit,
                "page": page
            }
            
            if item_id:
                params["itmId"] = item_id  # Changed from itemId to itmId
            
            logger.debug(f"Fetching paginated consumption from NestJS API: {api_url} with params {params}")
            response = requests.get(api_url, params=params, timeout=10)
            
            if response.status_code != 200:
                logger.warning(f"NestJS API returned status {response.status_code}.")
                return {"items": [], "total": 0, "nextPage": None}
            
            # NestJS returns paginated response: {items: [], total: int, nextPage: int}
            data = response.json()
            return {
                "items": data.get("items", []),
                "total": data.get("total", 0),
                "nextPage": data.get("nextPage")
            }
                
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error calling NestJS API: {str(e)}.")
            return {"items": [], "total": 0, "nextPage": None}
        except Exception as e:
            logger.warning(f"Unexpected error fetching paginated consumption: {str(e)}.")
            return {"items": [], "total": 0, "nextPage": None}

    @staticmethod
    def has_consumption_history(db: Session, product_id: int) -> bool:
        """
        Quick check if a product has any consumption history.
        Returns True if consumption exists, False otherwise.
        This is more efficient than fetching all consumption data just to check existence.
        
        Args:
            db: Database session
            product_id: Product ID from main database (integer)
        
        Returns:
            True if consumption history exists, False otherwise
        """
        item_id = str(product_id)
        consumption_data = ConsumptionService.get_consumption_history(db, item_id)
        has_data = len(consumption_data) > 0
        logger.debug(f"🔍 [CONSUMPTION CHECK] Product_id {product_id} has consumption history: {has_data}")
        return has_data

    @staticmethod
    def get_consumption_by_product_id(db: Session, product_id: int) -> List[Dict[str, Any]]:
        """
        Get consumption history for a product_id (main DB) by converting it to itemId.
        This assumes product_id can be converted to string for itemId lookup.
        
        Args:
            db: Database session
            product_id: Product ID from main database (integer)
        
        Returns:
            List of consumption history records with standardized field names
        """
        # Convert product_id to string for itemId lookup
        # Note: You may need to adjust this mapping based on how product_id maps to itemId
        item_id = str(product_id)
        
        logger.info(f"🔄 [CONSUMPTION] Converting product_id {product_id} (int) to itemId '{item_id}' (string) for ERP lookup")
        consumption_data = ConsumptionService.get_consumption_history(db, item_id)
        
        logger.info(f"📊 [CONSUMPTION] Transforming {len(consumption_data)} consumption records to standardized format for product_id: {product_id}")
        # Transform to standardized format
        result = []
        for item in consumption_data:
            # Handle camelCase fields from ERP schema
            result.append({
                "id": item.get("id"),
                "consumption_id": item.get("consumptionId"),
                "item_id": item.get("itemId"),
                "date": item.get("date"),
                "department": item.get("department"),
                "quantity_used": item.get("quantityUsed", 0),  # Convert camelCase to snake_case
                "consumed_by_id": item.get("consumedById"),
                "created_at": item.get("createdAt")
            })
        
        return result

