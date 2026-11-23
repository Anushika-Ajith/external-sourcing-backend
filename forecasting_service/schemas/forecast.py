from pydantic import BaseModel
from typing import List, Dict, Any


class ForecastResponse(BaseModel):
    product_id: int
    current_stock: int  # Current available stock from store_batch_stock
    metrics: Dict[str, float]
    test_predictions: List[Any]
    future_forecast: List[Any]
    feature_importance: Dict[str, float]

    class Config:
        orm_mode = True
