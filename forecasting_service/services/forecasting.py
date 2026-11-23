# import logging
# import pandas as pd
# import numpy as np
# from sklearn.ensemble import RandomForestRegressor
# from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, mean_squared_error
# from sqlalchemy.orm import Session
# import models
# from datetime import datetime, timedelta
# from typing import Dict, Any, List

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)

# class HospitalDemandForecaster:
#     def __init__(self, db: Session, item_code: str, user_id: int):
#         self.db = db
#         self.item_code = item_code
#         self.user_id = user_id
#         self.model = None

#     def load_data_from_db(self) -> pd.DataFrame:
#         """Load historical data from database with user isolation"""
#         try:
#             logger.info(f"Loading data for item: {self.item_code} (user: {self.user_id})")
#             item = self.db.query(models.Item).filter(
#                 models.Item.item_code == self.item_code,
#                 models.Item.user_id == self.user_id
#             ).first()
            
#             if not item:
#                 raise ValueError(f"Item with code {self.item_code} not found for user {self.user_id}")

#             history = self.db.query(models.HistoricalData).filter(
#                 models.HistoricalData.item_id == item.id,
#                 models.HistoricalData.user_id == self.user_id
#             ).order_by(models.HistoricalData.week).all()
            
#             if len(history) < 12:
#                 raise ValueError(f"Insufficient data: {len(history)} records found. Need at least 12 weeks.")

#             df = pd.DataFrame([{
#                 "Week": h.week,
#                 "Item_Code": item.item_code,
#                 "Item_Description": item.description,
#                 "Patient_Footfall": h.patient_footfall,
#                 "Last_Week_Usage": h.last_week_usage,
#                 "Public_Holiday": int(h.public_holiday),
#                 "Rain_Impact": int(h.rain_impact),
#                 "Quantity_Used": h.quantity_used
#             } for h in history])

#             df["Week"] = pd.to_datetime(df["Week"])
#             df.sort_values(["Item_Code", "Week"], inplace=True)
#             return df
        
#         except Exception as e:
#             logger.error(f"Error loading data: {str(e)}")
#             raise

#     def preprocess_data(self, df: pd.DataFrame) -> pd.DataFrame:
#         """Feature engineering and data cleaning"""
#         try:
#             df = df.copy()
#             df.rename(columns={'Quantity_Used': 'Quantity_used'}, inplace=True)
            
#             # Generate features
#             df['Lag_1'] = df.groupby('Item_Code')['Quantity_used'].shift(1)
#             df['Lag_2'] = df.groupby('Item_Code')['Quantity_used'].shift(2)
#             df['Lag_3'] = df.groupby('Item_Code')['Quantity_used'].shift(3)
            
#             df['Rolling_Mean_3'] = (
#                 df.groupby('Item_Code')['Quantity_used']
#                 .rolling(window=3, min_periods=1)
#                 .mean()
#                 .reset_index(level=0, drop=True))
            
#             df['Rolling_Mean_7'] = (
#                 df.groupby('Item_Code')['Quantity_used']
#                 .rolling(window=7, min_periods=1)
#                 .mean()
#                 .reset_index(level=0, drop=True))
            
#             df['week_index'] = df.groupby('Item_Code').cumcount() + 1
#             df['month'] = df['Week'].dt.month
#             df['quarter'] = df['Week'].dt.quarter
            
#             return df.dropna(subset=['Lag_1', 'Lag_2', 'Lag_3']) 
        
#         except Exception as e:
#             logger.error(f"Error in preprocessing: {str(e)}")
#             raise

#     def train_and_predict(self, df: pd.DataFrame) -> Dict[str, Any]:
#         """Train model and generate forecasts with proper user context"""
#         try:
#             # Verify item ownership
#             item = self.db.query(models.Item).filter(
#                 models.Item.item_code == self.item_code,
#                 models.Item.user_id == self.user_id
#             ).first()
            
#             if not item:
#                 raise ValueError("Item not found in database")                
            
#             item_data = df[df['Item_Code'] == self.item_code].copy()
#             total_weeks = len(item_data)
            
#             # Dynamic test size (20% but min 2, max 4 weeks)
#             test_weeks = min(4, max(2, total_weeks // 5))
#             train = item_data.iloc[:-test_weeks]
#             test = item_data.iloc[-test_weeks:]

#             if len(train) < 10:
#                 raise ValueError(f"Insufficient training data: {len(train)} records")

#             features = [
#                 'Patient_Footfall', 'Public_Holiday', 'Rain_Impact', 
#                 'Lag_1', 'Lag_2', 'Lag_3',
#                 'Rolling_Mean_3', 'Rolling_Mean_7',
#                 'month', 'quarter'
#             ]
#             available_features = [f for f in features if f in train.columns]
#             X_train = train[available_features]
#             y_train = train['Quantity_used']
#             X_test = test[available_features]

#             model = RandomForestRegressor(
#                 n_estimators=200,
#                 max_depth=10,
#                 min_samples_split=5,
#                 min_samples_leaf=2,
#                 random_state=42,
#                 n_jobs=-1
#             )
#             model.fit(X_train, y_train)
#             predictions = model.predict(X_test)
#             predictions = np.maximum(predictions, 0)  # Ensure non-negative
            
#             # Convert to native Python types
#             predictions = [float(x) for x in predictions]
#             test_actuals = [int(x) for x in test['Quantity_used']]

#             # Calculate metrics
#             metrics = {}
#             if test_actuals:
#                 metrics = {
#                     "MAE": round(mean_absolute_error(test_actuals, predictions), 2),
#                     "MAPE": round(mean_absolute_percentage_error(test_actuals, predictions) * 100, 2),
#                     "RMSE": round(np.sqrt(mean_squared_error(test_actuals, predictions)), 2),
#                     "Accuracy": round(100 - mean_absolute_percentage_error(test_actuals, predictions) * 100, 2)
#                 }

#             # Clear existing forecasts for this item
#             self.db.query(models.Forecast).filter(
#                 models.Forecast.item_id == item.id,
#                 models.Forecast.user_id == self.user_id
#             ).delete()
#             self.db.commit()

#             forecast_output = []
#             forecast_date = datetime.utcnow().date()

#             # Save test predictions
#             for i in range(len(test)):
#                 week_date = test.iloc[i]["Week"].date()
#                 actual = test_actuals[i]
#                 predicted = predictions[i]
                
#                 forecast_output.append({
#                     "week": str(week_date),
#                     "actual": actual,
#                     "predicted": round(predicted, 2)
#                 })

#                 forecast_record = models.Forecast(
#                     item_id=item.id,
#                     user_id=self.user_id,
#                     forecast_date=forecast_date,
#                     week=week_date,
#                     predicted_quantity=predicted,
#                     actual_quantity=actual,
#                     created_at=datetime.utcnow()
#                 )
#                 self.db.add(forecast_record)
            
#             self.db.commit()
            
#             # Generate future forecasts (4 weeks ahead)
#             future_forecasts = []
#             last_date = item_data['Week'].max()
            
#             # Train on full dataset
#             X_full = item_data[available_features]
#             y_full = item_data['Quantity_used']
#             model_full = RandomForestRegressor(
#                 n_estimators=200,
#                 max_depth=10,
#                 min_samples_split=5,
#                 min_samples_leaf=2,
#                 random_state=42,
#                 n_jobs=-1
#             )
#             model_full.fit(X_full, y_full)
            
#             # Calculate average patient footfall for future predictions
#             avg_footfall = item_data['Patient_Footfall'].mean()
            
#             for weeks_ahead in range(1, 5):
#                 future_week = last_date + timedelta(weeks=weeks_ahead)
#                 future_date = future_week.date()
                
#                 # Prepare features
#                 future_features = {
#                     'Patient_Footfall': avg_footfall,
#                     'Public_Holiday': 0,
#                     'Rain_Impact': 0,
#                     'Lag_1': item_data['Quantity_used'].iloc[-1],
#                     'Lag_2': item_data['Quantity_used'].iloc[-2] if len(item_data) >= 2 else item_data['Quantity_used'].iloc[-1],
#                     'Lag_3': item_data['Quantity_used'].iloc[-3] if len(item_data) >= 3 else item_data['Quantity_used'].iloc[-1],
#                     'Rolling_Mean_3': item_data['Quantity_used'].iloc[-3:].mean(),
#                     'Rolling_Mean_7': item_data['Quantity_used'].iloc[-7:].mean() if len(item_data) >= 7 else item_data['Quantity_used'].mean(),
#                     'month': future_week.month,
#                     'quarter': (future_week.month - 1) // 3 + 1
#                 }
                
#                 # Filter to available features
#                 filtered_features = {k: v for k, v in future_features.items() if k in available_features}
#                 X_future = pd.DataFrame([filtered_features], columns=available_features)
                
#                 predicted_qty = float(max(0, model_full.predict(X_future)[0]))
                
#                 future_forecasts.append({
#                     "week": str(future_date),
#                     "predicted": round(predicted_qty, 2)
#                 })
                
#                 forecast_record = models.Forecast(
#                     item_id=item.id,
#                     user_id=self.user_id,
#                     forecast_date=forecast_date,
#                     week=future_date,
#                     predicted_quantity=predicted_qty,
#                     created_at=datetime.utcnow()
#                 )
#                 self.db.add(forecast_record)
            
#             self.db.commit()
            
#             logger.info(f"Successfully generated forecasts for {self.item_code}")
            
#             return {
#                 "item": self.item_code,
#                 "description": item.description,
#                 "metrics": metrics,
#                 "historical_forecasts": forecast_output,
#                 "future_forecasts": future_forecasts,
#                 "feature_importance": dict(zip(available_features, model.feature_importances_))
#             }
        
#         except Exception as e:
#             logger.error(f"Error in training/prediction: {str(e)}", exc_info=True)
#             self.db.rollback()
#             raise ValueError(f"Forecast generation failed: {str(e)}")

#     def run(self) -> Dict[str, Any]:
#         """Main execution method with error handling"""
#         try:
#             df = self.load_data_from_db()
#             df_processed = self.preprocess_data(df)
#             return self.train_and_predict(df_processed)
#         except Exception as e:
#             logger.error(f"Forecast pipeline failed: {str(e)}", exc_info=True)
#             raise ValueError(f"Forecast pipeline failed: {str(e)}")



# import logging
# import json
# import pandas as pd
# import numpy as np
# from datetime import datetime, timedelta
# from typing import Dict, Any
# from sklearn.ensemble import RandomForestRegressor
# from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, mean_squared_error
# from sqlalchemy.orm import Session
# import models  # Make sure this includes Products & ConsumptionHistory tables

# logging.basicConfig(level=logging.INFO)
# logger = logging.getLogger(__name__)


# class HospitalDemandForecaster:
#     def __init__(self, db: Session, product_id: int):
#         self.db = db
#         self.product_id = product_id
#         self.model = None

#     def load_data_from_db(self) -> pd.DataFrame:
#         """ 
#         Load consumption history from NestJS API (ERP database) and transform to ML-ready format.
#         Uses consumption_history table from erp_db with schema:
#         - itemId (text) maps to product_id (int) in main DB
#         - quantityUsed (integer) - consumption quantity
#         - date (timestamp) - consumption date
        
#         Raises ValueError if no consumption history found - forecast should not proceed without data.
#         """
#         try:
#             logger.info(f"Loading consumption data for product_id: {self.product_id}")

#             print("id: ",self.product_id)

#             # Verify product exists in main DB
#             product = self.db.query(models.Product).filter(
#                 models.Product.product_id == self.product_id
#             ).first()

#             if not product:
#                 raise ValueError(f"Product ID {self.product_id} not found.")

#             # Import here to avoid circular dependency
#             from services.consumption_service import ConsumptionService
            
#             # Fetch consumption history from NestJS API (ERP database)
#             logger.info(f"🔍 [CONSUMPTION] Fetching consumption history from NestJS API for product_id: {self.product_id}")
#             consumption_data = ConsumptionService.get_consumption_by_product_id(self.db, self.product_id)
            
#             logger.info(f"✅ [CONSUMPTION] Received {len(consumption_data) if consumption_data else 0} consumption records from API for product_id: {self.product_id}")
            
#             if not consumption_data:
#                 error_msg = f"No consumption history found for product_id {self.product_id}. Skipping forecast - consumption data is required."
#                 logger.warning(f"⚠️ [CONSUMPTION] {error_msg}")
#                 raise ValueError(error_msg)

#             # Transform to DataFrame
#             df = pd.DataFrame([{
#                 "Week": pd.to_datetime(item["date"]) if item.get("date") else None,
#                 "Quantity_used": item.get("quantity_used", 0)
#             } for item in consumption_data])

#             # Remove rows with null dates
#             df = df.dropna(subset=["Week"])
            
#             if len(df) == 0:
#                 error_msg = f"No valid consumption records with dates for product_id {self.product_id}. Skipping forecast - valid consumption data is required."
#                 logger.warning(f"⚠️ [CONSUMPTION] {error_msg}")
#                 raise ValueError(error_msg)

#             df.sort_values("Week", inplace=True)
            
#             logger.info(f"✅ [CONSUMPTION] Loaded {len(df)} valid consumption records for product_id {self.product_id}")
#             return df

#         except ValueError:
#             # Re-raise ValueError (no consumption data) - caller should handle
#             raise
#         except Exception as e:
#             logger.error(f"❌ [CONSUMPTION] Data load error for product_id {self.product_id}: {str(e)}")
#             raise

#     def preprocess_data(self, df: pd.DataFrame) -> pd.DataFrame:
#         """ Add lag features and rolling averages """
#         try:
#             df = df.copy()

#             df['Lag_1'] = df['Quantity_used'].shift(1)
#             df['Lag_2'] = df['Quantity_used'].shift(2)
#             df['Lag_3'] = df['Quantity_used'].shift(3)

#             df['Rolling_Mean_3'] = df['Quantity_used'].rolling(3).mean()
#             df['Rolling_Mean_7'] = df['Quantity_used'].rolling(7).mean()

#             df['Month'] = df['Week'].dt.month
#             df['Quarter'] = df['Week'].dt.quarter

#             return df.dropna()

#         except Exception as e:
#             logger.error(f"Preprocessing error: {str(e)}")
#             raise

#     def train_and_predict(self, df: pd.DataFrame) -> Dict[str, Any]:
#         """ Train model and predict next 4 weeks """
#         try:
#             total_records = len(df)
#             test_size = max(2, min(4, total_records // 5))

#             train = df[:-test_size]
#             test = df[-test_size:]

#             features = ["Lag_1", "Lag_2", "Lag_3", "Rolling_Mean_3", "Rolling_Mean_7", "Month", "Quarter"]

#             X_train, y_train = train[features], train["Quantity_used"]
#             X_test = test[features]

#             model = RandomForestRegressor(n_estimators=200, random_state=42)
#             model.fit(X_train, y_train)

#             predictions = model.predict(X_test)
#             predictions = np.maximum(predictions, 0)

#             metrics = {
#                 "MAE": float(mean_absolute_error(test["Quantity_used"], predictions)),
#                 "MAPE": float(mean_absolute_percentage_error(test["Quantity_used"], predictions) * 100),
#                 "RMSE": float(np.sqrt(mean_squared_error(test["Quantity_used"], predictions))),
#                 "Accuracy": round(100 - (mean_absolute_percentage_error(test["Quantity_used"], predictions) * 100), 2)
#             }

#             # Future forecasting (next 4 weeks)
#             future = []
#             last_date = df["Week"].max()

#             model_full = RandomForestRegressor(n_estimators=200, random_state=42)
#             model_full.fit(df[features], df["Quantity_used"])

#             for i in range(1, 5):
#                 future_date = last_date + timedelta(weeks=i)
#                 input_row = df.iloc[-1:].copy()

#                 input_row["Week"] = future_date
#                 input_row["Month"] = future_date.month
#                 input_row["Quarter"] = (future_date.month - 1) // 3 + 1

#                 pred_value = float(model_full.predict(input_row[features])[0])

#                 future.append({
#                     "week": str(future_date.date()),
#                     "predicted": round(pred_value, 2)
#                 })

#             # Import here to avoid circular dependency
#             from services.stock_service import StockService
            
#             # Get current stock for the product
#             current_stock = StockService.get_current_stock(self.db, self.product_id)
#             print("CURRENT STOCK: ",current_stock)
#             # Save output to file
#             output = {
#                 "product_id": self.product_id,
#                 "current_stock": current_stock,
#                 "metrics": metrics,
#                 "test_predictions": list(zip(list(test["Week"].dt.strftime("%Y-%m-%d")), predictions)),
#                 "future_forecast": future,
#                 "feature_importance": dict(zip(features, model.feature_importances_))
#             }

#             filename = f"forecast_output_product_{self.product_id}_{datetime.utcnow().date()}.json"
#             with open(filename, "w") as f:
#                 json.dump(output, f, indent=4)

#             logger.info(f"Forecast saved to {filename}")

#             return output

#         except Exception as e:
#             logger.error(f"Training error: {str(e)}")
#             raise

#     def run(self):
#         """
#         Run the complete forecast pipeline.
#         Raises ValueError if no consumption history is found - forecast requires consumption data.
#         """
#         logger.info(f"▶️ [FORECAST] Starting forecast pipeline for product_id: {self.product_id}")
#         logger.info(f"📥 [FORECAST] Step 1/3: Loading consumption data from NestJS API (ERP database)...")
        
#         try:
#             df = self.load_data_from_db()
#         except ValueError as e:
#             # No consumption data - this is expected for products without history
#             logger.info(f"⏭️ [FORECAST] Skipping forecast for product_id {self.product_id}: {str(e)}")
#             raise
        
#         logger.info(f"🔄 [FORECAST] Step 2/3: Preprocessing {len(df)} consumption records...")
#         processed = self.preprocess_data(df)
#         logger.info(f"✅ [FORECAST] Preprocessing complete, {len(processed)} records ready for training")
        
#         logger.info(f"🤖 [FORECAST] Step 3/3: Training model and generating predictions...")
#         result = self.train_and_predict(processed)
#         logger.info(f"✅ [FORECAST] Forecast pipeline completed successfully for product_id: {self.product_id}")
        
#         return result

import logging
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, mean_squared_error
from sqlalchemy.orm import Session
import models

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class HospitalDemandForecaster:

    def __init__(self, db: Session, product_id: int):
        self.db = db
        self.product_id = product_id
        self.model = None

    def load_data_from_db(self) -> pd.DataFrame:
        try:
            logger.info(f"Loading consumption data for product_id: {self.product_id}")

            product = self.db.query(models.Product).filter(
                models.Product.product_id == self.product_id
            ).first()

            if not product:
                raise ValueError(f"Product ID {self.product_id} not found.")

            from services.consumption_service import ConsumptionService
            
            consumption_data = ConsumptionService.get_consumption_by_product_id(self.db, self.product_id)
            
            if not consumption_data:
                raise ValueError(f"No consumption history found for product_id {self.product_id}.")

            df = pd.DataFrame([{
                "Week": pd.to_datetime(item["date"]) if item.get("date") else None,
                "Quantity_used": item.get("quantity_used", 0)
            } for item in consumption_data])

            df = df.dropna(subset=["Week"])
            df.sort_values("Week", inplace=True)

            return df

        except Exception as e:
            logger.error(f"❌ Data load error for product_id {self.product_id}: {str(e)}")
            raise

    def preprocess_data(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            df = df.copy()
            df['Lag_1'] = df['Quantity_used'].shift(1)
            df['Lag_2'] = df['Quantity_used'].shift(2)
            df['Lag_3'] = df['Quantity_used'].shift(3)
            df['Rolling_Mean_3'] = df['Quantity_used'].rolling(3).mean()
            df['Rolling_Mean_7'] = df['Quantity_used'].rolling(7).mean()
            df['Month'] = df['Week'].dt.month
            df['Quarter'] = df['Week'].dt.quarter

            return df.dropna()

        except Exception as e:
            logger.error(f"Preprocessing error: {str(e)}")
            raise

    def train_and_predict(self, df: pd.DataFrame) -> Dict[str, Any]:
        try:
            total_records = len(df)
            test_size = max(2, min(4, total_records // 5))

            train = df[:-test_size]
            test = df[-test_size:]

            features = ["Lag_1", "Lag_2", "Lag_3", "Rolling_Mean_3", "Rolling_Mean_7", "Month", "Quarter"]

            X_train, y_train = train[features], train["Quantity_used"]
            X_test = test[features]

            model = RandomForestRegressor(n_estimators=200, random_state=42)
            model.fit(X_train, y_train)

            predictions = np.maximum(model.predict(X_test), 0)

            future = []
            last_date = df["Week"].max()

            model_full = RandomForestRegressor(n_estimators=200, random_state=42)
            model_full.fit(df[features], df["Quantity_used"])

            for i in range(1, 5):
                future_date = last_date + timedelta(weeks=i)
                input_row = df.iloc[-1:].copy()
                input_row["Week"] = future_date
                input_row["Month"] = future_date.month
                input_row["Quarter"] = (future_date.month - 1) // 3 + 1
                pred_value = float(model_full.predict(input_row[features])[0])

            future.append({
                "week": str(future_date.date()),
                "predicted": round(pred_value, 2)
            })

            # Log predicted values for each week
            logger.info(f"📊 [FORECAST] Predicted values for product_id {self.product_id}:")
            for i, forecast_item in enumerate(future, 1):
                logger.info(f"   Week {i} ({forecast_item['week']}): {forecast_item['predicted']} units")
            
            # Log the first week prediction (used for requirements)
            if future:
                first_week_prediction = future[0]["predicted"]
                logger.info(f"📈 [FORECAST] First week prediction for product_id {self.product_id}: {first_week_prediction} units")

            from services.stock_service import StockService
            try:
                current_stock = StockService.get_current_stock(self.db, self.product_id)
                # Ensure current_stock is an integer
                if not isinstance(current_stock, int):
                    current_stock = int(current_stock) if current_stock else 0
            except Exception as stock_error:
                logger.warning(f"⚠️ [FORECAST] Error fetching stock for product_id {self.product_id}: {str(stock_error)}. Using 0 as default.")
                current_stock = 0

            return {
                "product_id": self.product_id,
                "current_stock": int(current_stock),  # Ensure it's an integer
                "future_forecast": future
            }

        except Exception as e:
            logger.error(f"Training error for product_id {self.product_id}: {str(e)}", exc_info=True)
            raise
    def run(self):
        """
        Run the complete forecast pipeline.
        Raises ValueError if no consumption history is found - forecast requires consumption data.
        """
        logger.info(f"▶️ [FORECAST] Starting forecast pipeline for product_id: {self.product_id}")
        logger.info(f"📥 [FORECAST] Loading consumption data from NestJS API (ERP database)...")
        
        try:
            df = self.load_data_from_db()
        except ValueError as e:
            # No consumption data - this is expected for products without history
            logger.info(f"⏭️ [FORECAST] Skipping forecast for product_id {self.product_id}: {str(e)}")
            raise
        
        logger.info(f"🔄 [FORECAST] Preprocessing {len(df)} consumption records...")
        processed = self.preprocess_data(df)
        logger.info(f"✅ [FORECAST] Preprocessing complete, {len(processed)} records ready for training")
        
        logger.info(f"🤖 [FORECAST] Training model and generating predictions...")
        result = self.train_and_predict(processed)
        logger.info(f"✅ [FORECAST] Forecast pipeline completed successfully for product_id: {self.product_id}")
        
        return result
