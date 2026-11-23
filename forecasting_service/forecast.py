from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
import logging
from models import Item, Forecast, User
from database import get_db
from services.auth_utils import get_current_user
from .services.forecasting import HospitalDemandForecaster


router = APIRouter()
logger = logging.getLogger(__name__)

@router.post("/generate/{item_code}")
def generate_forecast(
    item_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(Item).filter(
        Item.item_code == item_code,
        Item.user_id == current_user.id  # Ownership check
    ).first()
    
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    
    forecaster = HospitalDemandForecaster(db, item_code, current_user.id)  # Pass user_id
    try:
        forecasts = forecaster.run()
        db.commit()  # Ensure changes are saved
        return forecasts
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))@router.get("/history/{item_code}")
def get_saved_forecasts(
    item_code: str,
    skip: int = 0,
    limit: int = 100, 
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    item = db.query(Item).filter(
        Item.item_code == item_code,
        Item.user_id == current_user.id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    forecasts = db.query(Forecast).filter(
        Forecast.item_id == item.id,
        Forecast.user_id == current_user.id
    ).order_by(Forecast.week).offset(skip).limit(limit).all()

    return [
        {
            "week": f.week,
            "predicted_quantity": f.predicted_quantity,
            "forecast_date": f.forecast_date,
            "created_at": f.created_at
        } for f in forecasts
    ]

@router.get("/dashboard-stats")
def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        total_items = db.query(Item).filter(
            Item.user_id == current_user.id
        ).count()
        
        total_forecasts = db.query(Forecast).filter(
            Forecast.user_id == current_user.id
        ).count()
        
        recent_forecasts = db.query(Forecast).filter(
            Forecast.created_at >= datetime.utcnow() - timedelta(days=7),
            Forecast.user_id == current_user.id
        ).count()
        
        accuracy_query = db.query(
            func.avg(100 - func.abs(
                (Forecast.predicted_quantity - Forecast.actual_quantity) / 
                Forecast.actual_quantity
            ) * 100)
        ).filter(
            Forecast.actual_quantity > 0,
            Forecast.user_id == current_user.id
        )
        
        avg_accuracy = accuracy_query.scalar() or 0
        
        return {
            "total_items": total_items,
            "total_forecasts": total_forecasts,
            "recent_forecasts": recent_forecasts,
            "avg_accuracy": round(float(avg_accuracy), 2),
            "last_updated": datetime.utcnow().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/items-list")
def get_items_list(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    items = db.query(Item).filter(
        Item.user_id == current_user.id
    ).all()
    return [
        {
            "code": item.item_code, 
            "description": item.description,
            "category": item.category,
            "department": item.department  
        } for item in items
    ]

@router.get("/accuracy")
def get_forecast_accuracy(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        items = db.query(Item).filter(
            Item.user_id == current_user.id
        ).join(Forecast).distinct().all()
        
        accuracy_data = []
        
        for item in items:
            forecasts = db.query(Forecast).filter(
                Forecast.item_id == item.id,
                Forecast.actual_quantity.isnot(None),
                Forecast.actual_quantity > 0,
                Forecast.user_id == current_user.id
            ).all()
            
            if not forecasts:
                continue
                
            errors = []
            for f in forecasts:
                if f.actual_quantity > 0:
                    error = abs(f.predicted_quantity - f.actual_quantity) / f.actual_quantity
                    errors.append(error)
            
            if errors:  
                mape = sum(errors) / len(errors) * 100
                accuracy_data.append({
                    "sku": item.item_code,
                    "description": item.description,
                    "accuracy": max(0, 100 - mape),
                    "mape": mape,
                    "forecast_count": len(forecasts)
                })
        
        accuracy_data.sort(key=lambda x: x["accuracy"], reverse=True)
        return accuracy_data
        
    except Exception as e:
        logger.error(f"Error calculating accuracy: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))