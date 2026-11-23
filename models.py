# from sqlalchemy import Column, Integer, String, Date, Boolean, ForeignKey, Float, Text
# from sqlalchemy import DateTime, func, Index, UniqueConstraint
# from datetime import datetime
# from sqlalchemy.orm import relationship
# from database import Base
# from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Date, Numeric, Text, DateTime, BigInteger
# from sqlalchemy.orm import relationship
# from sqlalchemy.sql import func
# from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
# from sqlalchemy.orm import relationship
# from datetime import datetime
# from sqlalchemy.dialects.postgresql import JSON


# # from app.models.product import Product

# # class User(Base):
# #     __tablename__ = "users"
# #     id = Column(Integer, primary_key=True, index=True)
# #     email = Column(String, unique=True, index=True)
# #     hashed_password = Column(String)
# #     role = Column(String, default="user")
# #     is_active = Column(Boolean, default=True)
# #     created_at = Column(DateTime, server_default=func.now())
# #     updated_at = Column(DateTime, onupdate=datetime.utcnow())
# #     vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=True)  # Make nullable
    
# #     vendor = relationship("Vendor", back_populates="user_account", foreign_keys=[vendor_id])
    
# #     products = relationship("Product", backref="creator")
# #     carts = relationship("Cart", backref="user")
# #     orders = relationship("Order", back_populates="user", foreign_keys="Order.user_id")
# #     items = relationship("Item", back_populates="user", cascade="all, delete-orphan")
# #     historical_data = relationship("HistoricalData", back_populates="user", cascade="all, delete-orphan")
# #     forecasts = relationship("Forecast", back_populates="user", cascade="all, delete-orphan")
# #     invites_created = relationship("Invite", back_populates="creator", cascade="all, delete-orphan")
# #     audit_logs = relationship("AuditLog", back_populates="user")

# class User(Base):
#     __tablename__ = "users"

#     user_id = Column(Integer, primary_key=True, index=True)
#     email = Column(String, unique=True, index=True)
#     hashed_password = Column(String)
#     role = Column(String)
#     is_active = Column(Boolean)

#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, onupdate=datetime.utcnow)

#     # vendor_id = Column(Integer, ForeignKey("vendors.vendor_id"), nullable=True)

#     # Audit columns
#     created_by_a = Column(String(50))
#     created_on_a = Column(DateTime, server_default=func.now())
#     created_ip_a = Column(String(45))
#     modified_by_a = Column(String(50))
#     modified_on_a = Column(DateTime)
#     modified_ip_a = Column(String(45))

#     # Only keep relationship, do NOT edit Vendor model now
#     # vendor = relationship("Vendor", back_populates="user_account", foreign_keys=[vendor_id])

#     # carts = relationship("Cart", backref="user")
#     carts = relationship("Cart", back_populates="user")
#     orders = relationship("Order", back_populates="user", foreign_keys="Order.user_id")
#     items = relationship("Item", back_populates="user")
#     historical_data = relationship("HistoricalData", back_populates="user")
#     forecasts = relationship("Forecast", back_populates="user")
#     invites_created = relationship("Invite", back_populates="creator")
#     audit_logs = relationship("AuditLog", back_populates="user")



# class Invite(Base):
#     __tablename__ = "invites"
#     id = Column(Integer, primary_key=True)
#     code = Column(String(12), unique=True)
#     is_admin = Column(Boolean, default=False)
#     used = Column(Boolean, default=False)
#     created_by = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"))
#     created_at = Column(DateTime, server_default=func.now())

    
#     creator = relationship("User", back_populates="invites_created")

# # class Item(Base):
# #     __tablename__ = 'items'
# #     id = Column(Integer, primary_key=True, index=True)
# #     item_code = Column(String(20), index=True)  # Removed unique constraint
# #     description = Column(String(100))
# #     department = Column(String(50))
# #     user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
# #     unit_price = Column(Float)
# #     category = Column(String(50))
# #     current_stock = Column(Integer)
# #     lead_time = Column(Integer)
# #     created_at = Column(DateTime, server_default=func.now())  
    
# #     forecasts = relationship("Forecast", back_populates="item", cascade="all, delete-orphan")
# #     historical_data = relationship("HistoricalData", back_populates="item", cascade="all, delete-orphan")
# #     user = relationship("User", back_populates="items")

# #     # Add composite unique constraint with user_id
# #     __table_args__ = (
# #         UniqueConstraint('item_code', 'user_id', name='uq_item_user'),
# #     )

# class Item(Base):
#     __tablename__ = "items"

#     item_id = Column(Integer, primary_key=True, index=True)
#     item_code = Column(String(20), index=True)
#     description = Column(String(100))
#     department = Column(String(50))

#     user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)

#     unit_price = Column(Float)
#     category = Column(String(50))
#     current_stock = Column(Integer)
#     lead_time = Column(Integer)

#     created_at = Column(DateTime, server_default=func.now())

#     # Relationships
#     user = relationship("User", back_populates="items")
#     forecasts = relationship("Forecast", back_populates="item", cascade="all, delete-orphan")
#     historical_data = relationship("HistoricalData", back_populates="item", cascade="all, delete-orphan")

#     __table_args__ = (
#         UniqueConstraint("item_code", "user_id", name="uq_item_user"),
#     )


# class Forecast(Base):
#     __tablename__ = 'forecasts'
#     id = Column(Integer, primary_key=True, index=True)
#     item_id = Column(Integer, ForeignKey('items.item_id', ondelete="CASCADE"), index=True)
#     user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
#     forecast_date = Column(Date)
#     week = Column(Date)
#     predicted_quantity = Column(Float)
#     actual_quantity = Column(Float, nullable=True)
#     created_at = Column(DateTime, default=datetime.utcnow)
#     confidence_score = Column(Float)  # Added confidence metric
    
#     item = relationship("Item", back_populates="forecasts")
#     user = relationship("User", back_populates="forecasts")

# class HistoricalData(Base):
#     __tablename__ = 'historical_data'
#     id = Column(Integer, primary_key=True, index=True)
#     item_id = Column(Integer, ForeignKey('items.item_id', ondelete="CASCADE"), index=True)
#     user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
#     week = Column(Date)
#     patient_footfall = Column(Integer)
#     last_week_usage = Column(Integer)
#     public_holiday = Column(Boolean)
#     rain_impact = Column(Boolean)
#     quantity_used = Column(Integer)
#     recorded_at = Column(DateTime, server_default=func.now())  # Added recording timestamp
    
#     item = relationship("Item", back_populates="historical_data")
#     user = relationship("User", back_populates="historical_data")

#     # Add composite index for frequent queries
#     __table_args__ = (
#         Index('idx_item_week', 'item_id', 'week'),
#     )

# # class Product(Base):
# #     __tablename__ = 'products'
# #     id = Column(Integer, primary_key=True)
# #     name = Column(String(100))
# #     description = Column(Text)
# #     price = Column(Float)
# #     category = Column(String(50))
# #     image_url = Column(String)
# #     stock_quantity = Column(Integer)
# #     is_active = Column(Boolean, default=True)
# #     created_at = Column(DateTime, default=datetime.utcnow)
# #     updated_at = Column(DateTime, onupdate=datetime.utcnow, nullable=True)
# #     created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
# # 
# class Product(Base):
#     __tablename__ = "products"

#     product_id = Column(Integer, primary_key=True, index=True)
#     name = Column(String(100), nullable=False)
#     description = Column(Text, nullable=True)
#     category = Column(String(50), nullable=True)
#     image_url = Column(String, nullable=True)
#     is_active = Column(Boolean, default=True)

#     # FK based on DB dump → references users.user_id
#     created_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)

#     # HSN details
#     hsn_code = Column(String(15), nullable=False)
#     hsn_description = Column(Text, nullable=True)

#     # JSON Specs
#     specs = Column(JSON, default={})
#     specs_version = Column(String(32), nullable=True)
#     specs_updated_at = Column(DateTime, server_default=func.now())

#     # Timestamps
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

#     # Audit fields from DB
#     created_by_a = Column(String(50), nullable=True)
#     created_on_a = Column(DateTime, server_default=func.now())
#     created_ip_a = Column(String(45), nullable=True)

#     modified_by_a = Column(String(50), nullable=True)
#     modified_on_a = Column(DateTime, nullable=True)
#     modified_ip_a = Column(String(45), nullable=True)

#     # Relationships
#     creator = relationship("User", backref="products")
#     suppliers = relationship("ProductSupplier", back_populates="product")

#     stock_entries = relationship("StockLedger", back_populates="product")
#     consumptions = relationship("ConsumptionHistory", back_populates="product")
  
# class Supplier(Base):
#     __tablename__ = "suppliers"

#     supplier_id = Column(Integer, primary_key=True, index=True)
#     name = Column(String(255), nullable=False)
#     email = Column(String(255), nullable=True)
#     phone = Column(String(50), nullable=True)
#     address = Column(Text, nullable=True)
#     gst_number = Column(String(50), nullable=True)
#     contact_person = Column(String(100), nullable=True)

#     is_active = Column(Boolean, default=True)
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

#     # Relationship for next module: product_suppliers
#     products = relationship("ProductSupplier", back_populates="supplier")
# class ProductSupplier(Base):
#     __tablename__ = "product_suppliers"

#     product_supplier_id = Column(Integer, primary_key=True, index=True)

#     product_id = Column(Integer, ForeignKey("products.product_id"), nullable=False)
#     supplier_id = Column(Integer, ForeignKey("suppliers.supplier_id"), nullable=False)

#     price = Column(Float, nullable=True)
#     currency = Column(String(10), default="INR")

#     lead_time_days = Column(Integer, nullable=True)
#     min_order_qty = Column(Integer, nullable=True)

#     is_preferred = Column(Boolean, default=False)
#     is_active = Column(Boolean, default=True)

#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

#     # Relationships
#     product = relationship("Product", back_populates="suppliers")
#     supplier = relationship("Supplier", back_populates="products")

#     __table_args__ = (
#         UniqueConstraint("product_id", "supplier_id", name="uq_product_supplier"),
#     )

# class Cart(Base):
#     __tablename__ = "carts"

#     cart_id = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)

#     status = Column(String(20), default="active")  # active / abandoned / completed
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

#     items = relationship("CartItem", back_populates="cart", cascade="all, delete-orphan")
#     user = relationship("User", back_populates="carts")


# # class CartItem(Base):
# #     __tablename__ = "cart_items"

# #     cart_item_id = Column(Integer, primary_key=True, index=True)

# #     cart_id = Column(Integer, ForeignKey("carts.cart_id"), nullable=False)
# #     product_id = Column(Integer, ForeignKey("products.product_id"), nullable=False)

# #     quantity = Column(Integer, default=1)
# #     added_at = Column(DateTime, server_default=func.now())

# #     cart = relationship("Cart", back_populates="items")
# #     product = relationship("Product")




# # class CartItem(Base):
# #     __tablename__ = 'cart_items'
# #     id = Column(Integer, primary_key=True)
# #     cart_id = Column(Integer, ForeignKey('carts.id'))
# #     product_id = Column(Integer, ForeignKey('products.id'))
# #     quantity = Column(Integer, default=1)
# #     added_at = Column(DateTime, server_default=func.now())
# #     cart = relationship("Cart", back_populates="items")
# #     product = relationship("Product")

# class CartItem(Base):
#     __tablename__ = "cart_items"

#     cart_item_id = Column(Integer, primary_key=True, index=True)

#     cart_id = Column(Integer, ForeignKey("carts.cart_id"), nullable=False)
#     product_id = Column(Integer, ForeignKey("products.product_id"), nullable=False)

#     quantity = Column(Integer, default=1)

#     added_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

#     cart = relationship("Cart", back_populates="items")
#     product = relationship("Product")

#     __table_args__ = (
#         UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),
#     )



# ### 2. **Order Schemas*

# # class Order(Base):
# #     __tablename__ = 'orders'
# #     id = Column(Integer, primary_key=True)
# #     user_id = Column(Integer, ForeignKey('users.id'))
# #     cart_id = Column(Integer, ForeignKey('carts.id'))
# #     status = Column(String(20), default='pending')  # pending/approved/rejected/completed
# #     total_amount = Column(Float)
# #     notes = Column(Text, nullable=True)
# #     approved_by = Column(Integer, ForeignKey('users.id'), nullable=True)
# #     approved_at = Column(DateTime, nullable=True)
# #     created_at = Column(DateTime, server_default=func.now())
# #     updated_at = Column(DateTime, onupdate=datetime.utcnow)
    
# #     user = relationship("User", back_populates="orders", foreign_keys=[user_id])
# #     cart = relationship("Cart")
# #     approver = relationship("User", foreign_keys=[approved_by])
# #     items = relationship("OrderItem", back_populates="order")

# class Order(Base):
#     __tablename__ = "orders"

#     order_id = Column(Integer, primary_key=True, index=True)

#     user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
#     cart_id = Column(Integer, ForeignKey("carts.cart_id"), nullable=False)

#     status = Column(String(20), default="pending")  # pending/approved/rejected/completed
#     total_amount = Column(Float, nullable=True)

#     notes = Column(Text, nullable=True)

#     approved_by = Column(Integer, ForeignKey("users.user_id"), nullable=True)
#     approved_at = Column(DateTime, nullable=True)

#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

#     user = relationship("User", foreign_keys=[user_id], back_populates="orders")
#     approver = relationship("User", foreign_keys=[approved_by])
#     cart = relationship("Cart")

#     items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")



# # class OrderItem(Base):
# #     __tablename__ = 'order_items'
# #     id = Column(Integer, primary_key=True)
# #     order_id = Column(Integer, ForeignKey('orders.id'))
# #     product_id = Column(Integer, ForeignKey('products.id'))
# #     quantity = Column(Integer)
# #     unit_price = Column(Float)
# #     total_price = Column(Float)
    
# #     order = relationship("Order", back_populates="items")
# #     product = relationship("Product")

# class OrderItem(Base):
#     __tablename__ = "order_items"

#     order_item_id = Column(Integer, primary_key=True, index=True)

#     order_id = Column(Integer, ForeignKey("orders.order_id"), nullable=False)
#     product_id = Column(Integer, ForeignKey("products.product_id"), nullable=False)

#     quantity = Column(Integer, nullable=False)
#     unit_price = Column(Float, nullable=True)
#     total_price = Column(Float, nullable=True)

#     order = relationship("Order", back_populates="items")
#     product = relationship("Product")



# # Add these imports at the top if not already present

# # Add these models after your existing models
# class Vendor(Base):
#     __tablename__ = "vendors"
    
#     id = Column(Integer, primary_key=True, index=True)
#     email = Column(String, unique=True, index=True)
#     mobile = Column(String(15), unique=True, index=True)
#     company_name = Column(String(255))
#     vendor_type = Column(String(50))  # Service, Product, Trading, Mixed
#     gst_number = Column(String(50))
#     pan_number = Column(String(50))
#     contact_person = Column(String(100))
#     address_line1 = Column(String(255))
#     address_line2 = Column(String(255))
#     city = Column(String(100))
#     state = Column(String(100))
#     country = Column(String(100), default="India")
#     postal_code = Column(String(20))
#     is_branch = Column(Boolean, default=False)
#     parent_vendor_id = Column(Integer, ForeignKey("vendors.id"))
#     status = Column(String(20), default="pending")  # pending, approved, rejected, clarification
#     created_at = Column(DateTime, server_default=func.now())
#     updated_at = Column(DateTime, onupdate=func.now())
    
#     # Relationships
#     banks = relationship("VendorBank", back_populates="vendor", cascade="all, delete-orphan")
#     items = relationship("VendorItem", back_populates="vendor", cascade="all, delete-orphan")
#     documents = relationship("VendorDocument", back_populates="vendor", cascade="all, delete-orphan")
#     parent = relationship("Vendor", remote_side=[id], backref="branches")
#     user_account = relationship("User", back_populates="vendor", uselist=False)  # One-to-one relationship

# class VendorBank(Base):
#     __tablename__ = "vendor_banks"
    
#     id = Column(Integer, primary_key=True, index=True)
#     vendor_id = Column(Integer, ForeignKey('vendors.id'), nullable=False)
#     bank_name = Column(String(255), nullable=False)
#     account_number = Column(String(50), nullable=False)
#     ifsc_code = Column(String(20), nullable=False)
#     branch_name = Column(String(100), nullable=False)
#     account_type = Column(String(20), nullable=False)  # 'Savings', 'Current', 'OD'
#     is_primary = Column(Boolean, default=False)
#     created_at = Column(DateTime, default=datetime.utcnow)
#     updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
#     vendor = relationship("Vendor", back_populates="banks")

# class VendorItem(Base):
#     __tablename__ = "vendor_items"
    
#     id = Column(Integer, primary_key=True, index=True)
#     vendor_id = Column(Integer, ForeignKey("vendors.id"))
#     item_name = Column(String(255))
#     item_type = Column(String(50))  # Service, Product
#     uom = Column(String(50))  # Unit of Measure
#     description = Column(Text)
    
#     vendor = relationship("Vendor", back_populates="items")
#     rates = relationship("VendorRate", back_populates="item", cascade="all, delete-orphan")

# class VendorRate(Base):
#     __tablename__ = "vendor_rates"
    
#     id = Column(Integer, primary_key=True, index=True)
#     item_id = Column(Integer, ForeignKey("vendor_items.id"))
#     rate = Column(Numeric(12, 2))
#     currency = Column(String(10), default="INR")
#     valid_from = Column(Date)
#     valid_to = Column(Date)
#     status = Column(String(20), default="pending")  # pending, approved, rejected
#     created_at = Column(DateTime, server_default=func.now())
    
#     item = relationship("VendorItem", back_populates="rates")

# class VendorDocument(Base):
#     __tablename__ = "vendor_documents"
    
#     id = Column(Integer, primary_key=True, index=True)
#     vendor_id = Column(Integer, ForeignKey("vendors.id"))
#     document_type = Column(String(100))  # GST, PAN, Registration, MSME, Other
#     document_number = Column(String(100))
#     issue_date = Column(Date)
#     expiry_date = Column(Date)
#     document_path = Column(String(255))
#     status = Column(String(20), default="pending")  # pending, approved, rejected
#     uploaded_at = Column(DateTime, server_default=func.now())
    
#     vendor = relationship("Vendor", back_populates="documents")

# class VendorAudit(Base):
#     __tablename__ = "vendor_audit_logs"
    
#     id = Column(Integer, primary_key=True, index=True)
#     table_name = Column(String(100))
#     record_id = Column(Integer)
#     field_name = Column(String(100))
#     old_value = Column(Text)
#     new_value = Column(Text)
#     action = Column(String(20))  # INSERT, UPDATE, DELETE
#     changed_by = Column(String(150))
#     changed_at = Column(DateTime, server_default=func.now())

# # Add OTP model
# class OTP(Base):
#     __tablename__ = "otps"
    
#     id = Column(Integer, primary_key=True, index=True)
#     email = Column(String, index=True)
#     mobile = Column(String(15), index=True)
#     otp_code = Column(String(6))
#     otp_type = Column(String(20))  # registration, login, reset
#     is_used = Column(Boolean, default=False)
#     expires_at = Column(DateTime)
#     created_at = Column(DateTime, server_default=func.now())

# class AuditLog(Base):
#     """
#     Model for tracking all changes made to vendor data
#     """
#     __tablename__ = "audit_logs"
    
#     id = Column(Integer, primary_key=True, index=True)
#     table_name = Column(String(50), nullable=False)  # e.g., 'vendors', 'vendor_banks'
#     record_id = Column(Integer, nullable=False)  # ID of the affected record
#     action = Column(String(10), nullable=False)  # 'CREATE', 'UPDATE', 'DELETE'
#     field_name = Column(String(50), nullable=True)  # Field that was changed
#     old_value = Column(Text, nullable=True)  # Previous value
#     new_value = Column(Text, nullable=True)  # New value
#     changed_by = Column(Integer, ForeignKey('users.user_id'), nullable=False)  # User who made the change
#     changed_at = Column(DateTime, default=datetime.utcnow)
    
#     # Relationships
#     user = relationship("User", back_populates="audit_logs")


# class StockLedger(Base):
#     __tablename__ = "stock_ledger"

#     id = Column(Integer, primary_key=True, index=True)
#     product_id = Column(Integer, ForeignKey("products.product_id"))
#     date = Column(DateTime, default=datetime.utcnow)
#     transaction_type = Column(String)   # RECEIPT | ISSUE | ADJUSTMENT
#     quantity = Column(Integer)
#     balance_after = Column(Integer)
#     reference = Column(String, nullable=True)
#     created_at = Column(DateTime, default=datetime.utcnow)
#     updated_at = Column(DateTime)

#     product = relationship("Product", back_populates="stock_entries")


# Product.stock_entries = relationship("StockLedger", back_populates="product")


# class ConsumptionHistory(Base):
#     __tablename__ = "consumption_history"

#     id = Column(Integer, primary_key=True, index=True)
#     product_id = Column(Integer, ForeignKey("products.product_id"))
#     date = Column(DateTime, default=datetime.utcnow)
#     department = Column(String)
#     quantity_used = Column(Integer)
#     consumed_by = Column(Integer)
#     created_at = Column(DateTime, default=datetime.utcnow)

#     product = relationship("Product", back_populates="consumptions")


# Product.consumptions = relationship("ConsumptionHistory", back_populates="product")

from sqlalchemy import (
    Column, Integer, String, Date, Boolean, ForeignKey, Float, Text,
    DateTime, Numeric, BigInteger, UniqueConstraint, Index, CheckConstraint, CHAR
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime
from sqlalchemy.dialects.postgresql import JSON
from database import Base, VectorBase
from sqlalchemy import Column, Integer, Text, Date, String, TIMESTAMP, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from pgvector.sqlalchemy import Vector

from sqlalchemy import ForeignKeyConstraint, text
# ---------------------------------------------------------
# USER MODEL (CLEANED – NO VENDOR RELATION)
# ---------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String)
    is_active = Column(Boolean)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, onupdate=datetime.utcnow)

    # Vendor relationship
    vendor_id = Column(Integer, ForeignKey("vendors.vendor_id"), nullable=True)

    # Audit fields
    created_by_a = Column(String(50))
    created_on_a = Column(DateTime, server_default=func.now())
    created_ip_a = Column(String(45))
    modified_by_a = Column(String(50))
    modified_on_a = Column(DateTime)
    modified_ip_a = Column(String(45))

    # Relationships
    carts = relationship("Cart", back_populates="user")
    orders = relationship("Order", back_populates="user", foreign_keys="Order.user_id")
    items = relationship("Item", back_populates="user")
    historical_data = relationship("HistoricalData", back_populates="user")
    forecasts = relationship("Forecast", back_populates="user")
    invites_created = relationship("Invite", back_populates="creator")
    audit_logs = relationship("AuditLog", back_populates="user")
    vendor = relationship("Vendor", back_populates="user_account", foreign_keys=[vendor_id], uselist=False)


# ---------------------------------------------------------
# INVITES
# ---------------------------------------------------------
class Invite(Base):
    __tablename__ = "invites"

    id = Column(Integer, primary_key=True)
    code = Column(String(12), unique=True)
    is_admin = Column(Boolean, default=False)
    used = Column(Boolean, default=False)
    created_by = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"))
    created_at = Column(DateTime, server_default=func.now())

    creator = relationship("User", back_populates="invites_created")


# ---------------------------------------------------------
# ITEMS
# ---------------------------------------------------------
class Item(Base):
    __tablename__ = "items"

    item_id = Column(Integer, primary_key=True, index=True)
    item_code = Column(String(20), index=True)
    description = Column(String(100))
    department = Column(String(50))

    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)

    unit_price = Column(Float)
    category = Column(String(50))
    current_stock = Column(Integer)
    lead_time = Column(Integer)

    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="items")
    forecasts = relationship("Forecast", back_populates="item", cascade="all, delete-orphan")
    historical_data = relationship("HistoricalData", back_populates="item", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("item_code", "user_id", name="uq_item_user"),
    )


# ---------------------------------------------------------
# FORECAST
# ---------------------------------------------------------
class Forecast(Base):
    __tablename__ = 'forecasts'

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.item_id', ondelete="CASCADE"))
    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"))

    forecast_date = Column(Date)
    week = Column(Date)
    predicted_quantity = Column(Float)
    actual_quantity = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    confidence_score = Column(Float)

    item = relationship("Item", back_populates="forecasts")
    user = relationship("User", back_populates="forecasts")


# ---------------------------------------------------------
# HISTORICAL DATA
# ---------------------------------------------------------
class HistoricalData(Base):
    __tablename__ = 'historical_data'

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey('items.item_id', ondelete="CASCADE"))
    user_id = Column(Integer, ForeignKey("users.user_id", ondelete="CASCADE"))

    week = Column(Date)
    patient_footfall = Column(Integer)
    last_week_usage = Column(Integer)
    public_holiday = Column(Boolean)
    rain_impact = Column(Boolean)
    quantity_used = Column(Integer)
    recorded_at = Column(DateTime, server_default=func.now())

    item = relationship("Item", back_populates="historical_data")
    user = relationship("User", back_populates="historical_data")


# ---------------------------------------------------------
# PRODUCT
# ---------------------------------------------------------
class Product(Base):
    __tablename__ = "products"

    product_id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100))
    description = Column(Text)
    category = Column(String(50))
    image_url = Column(String)
    is_active = Column(Boolean, default=True)

    created_by = Column(Integer, ForeignKey("users.user_id"))

    hsn_code = Column(String(15))
    hsn_description = Column(Text)

    specs = Column(JSON, default={})
    specs_version = Column(String(32))
    specs_updated_at = Column(DateTime, server_default=func.now())

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    created_by_a = Column(String(50))
    created_on_a = Column(DateTime, server_default=func.now())
    created_ip_a = Column(String(45))

    modified_by_a = Column(String(50))
    modified_on_a = Column(DateTime)
    modified_ip_a = Column(String(45))

    creator = relationship("User", backref="products")
    suppliers = relationship("ProductSupplier", back_populates="product")

    stock_entries = relationship("StockLedger", back_populates="product")
    consumptions = relationship("ConsumptionHistory", back_populates="product")


# ---------------------------------------------------------
# SUPPLIERS (NORMAL)
# ---------------------------------------------------------
class Supplier(Base):
    __tablename__ = "suppliers"

    supplier_id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255))
    phone = Column(String(50))
    address = Column(Text)
    gst_number = Column(String(50))
    contact_person = Column(String(100))

    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    products = relationship("ProductSupplier", back_populates="supplier")


# ---------------------------------------------------------
# PRODUCT SUPPLIER LINK TABLE
# ---------------------------------------------------------
class ProductSupplier(Base):
    __tablename__ = "product_suppliers"

    product_supplier_id = Column(Integer, primary_key=True)

    product_id = Column(Integer, ForeignKey("products.product_id"))
    supplier_id = Column(Integer, ForeignKey("suppliers.supplier_id"))

    # FIX → map Python field `price` to DB column `last_purchase_price`
    price = Column("last_purchase_price", Float)

    currency = Column(String(10), default="INR")

    lead_time_days = Column(Integer)
    min_order_qty = Column(Integer)

    is_preferred = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    product = relationship("Product", back_populates="suppliers")
    supplier = relationship("Supplier", back_populates="products")

    __table_args__ = (
        UniqueConstraint("product_id", "supplier_id", name="uq_product_supplier"),
    )


# ---------------------------------------------------------
# CART
# ---------------------------------------------------------
class Cart(Base):
    __tablename__ = "carts"

    cart_id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)

    status = Column(String(20), default="active")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    items = relationship("CartItem", back_populates="cart", cascade="all, delete-orphan")
    user = relationship("User", back_populates="carts")


# ---------------------------------------------------------
# CART ITEM
# ---------------------------------------------------------
class CartItem(Base):
    __tablename__ = "cart_items"

    cart_item_id = Column(Integer, primary_key=True)

    cart_id = Column(Integer, ForeignKey("carts.cart_id"))
    product_id = Column(Integer, ForeignKey("products.product_id"))

    quantity = Column(Integer, default=1)

    added_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    cart = relationship("Cart", back_populates="items")
    product = relationship("Product")

    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),
    )


# ---------------------------------------------------------
# ORDERS
# ---------------------------------------------------------
class Order(Base):
    __tablename__ = "orders"

    order_id = Column(Integer, primary_key=True)

    user_id = Column(Integer, ForeignKey("users.user_id"))
    cart_id = Column(Integer, ForeignKey("carts.cart_id"))

    status = Column(String(20), default="pending")
    total_amount = Column(Float)
    notes = Column(Text)

    approved_by = Column(Integer, ForeignKey("users.user_id"))
    approved_at = Column(DateTime)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", foreign_keys=[user_id], back_populates="orders")
    approver = relationship("User", foreign_keys=[approved_by])
    cart = relationship("Cart")

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


# ---------------------------------------------------------
# ORDER ITEM
# ---------------------------------------------------------
class OrderItem(Base):
    __tablename__ = "order_items"

    order_item_id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.order_id"))
    product_id = Column(Integer, ForeignKey("products.product_id"))

    quantity = Column(Integer)
    unit_price = Column(Float)
    total_price = Column(Float)

    order = relationship("Order", back_populates="items")
    product = relationship("Product")


# ---------------------------------------------------------
# OTP MODEL
# ---------------------------------------------------------
class OTP(Base):
    __tablename__ = "otps"

    id = Column(Integer, primary_key=True)
    email = Column(String, index=True)
    mobile = Column(String(15), index=True)
    otp_code = Column(String(6))
    otp_type = Column(String(20))
    is_used = Column(Boolean, default=False)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, server_default=func.now())


# ---------------------------------------------------------
# AUDIT LOG
# ---------------------------------------------------------
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    table_name = Column(String(50))
    record_id = Column(Integer)
    action = Column(String(10))
    field_name = Column(String(50))
    old_value = Column(Text)
    new_value = Column(Text)
    changed_by = Column(Integer, ForeignKey("users.user_id"))
    changed_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="audit_logs")


# ---------------------------------------------------------
# STOCK LEDGER
# ---------------------------------------------------------
class StockLedger(Base):
    __tablename__ = "stock_ledger"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.product_id"))

    date = Column(DateTime, default=datetime.utcnow)
    transaction_type = Column(String)
    quantity = Column(Integer)
    balance_after = Column(Integer)
    reference = Column(String)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime)

    product = relationship("Product", back_populates="stock_entries")


# ---------------------------------------------------------
# CONSUMPTION HISTORY
# ---------------------------------------------------------
class ConsumptionHistory(Base):
    __tablename__ = "consumption_history"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.product_id"))
    date = Column(DateTime, default=datetime.utcnow)

    department = Column(String)
    quantity_used = Column(Integer)
    consumed_by = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    product = relationship("Product", back_populates="consumptions")


# ---------------------------------------------------------
# REQUISITION HEADER
# ---------------------------------------------------------
class RequisitionHeader(Base):
    __tablename__ = "requisition_header"

    req_id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    document_type = Column(String(100))
    requisition_no = Column(Integer, nullable=False)
    revision_no = Column(Integer, default=0)
    requisition_date = Column(Date, nullable=False)
    department = Column(String(150))
    requester_id = Column(Integer, nullable=False)
    status = Column(String(50), default="DRAFT")
    priority = Column(String(50))
    source_type = Column(String(100))
    remarks = Column(String(500))
    
    approved_by = Column(Integer)
    approved_at = Column(DateTime(timezone=True))
    rejected_by = Column(Integer)
    rejected_at = Column(DateTime(timezone=True))
    rejection_reason = Column(String(500))
    
    pr_generated = Column(Boolean, default=False)
    pr_generated_at = Column(DateTime(timezone=True))
    po_generated = Column(Boolean, default=False)
    po_generated_at = Column(DateTime(timezone=True))
    
    created_by = Column(Integer, nullable=False)
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    lines = relationship("RequisitionLine", back_populates="header", cascade="all, delete-orphan")


# ---------------------------------------------------------
# REQUISITION LINE
# ---------------------------------------------------------
class RequisitionLine(Base):
    __tablename__ = "requisition_line"

    line_id = Column(Integer, primary_key=True, index=True)
    tenant_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    req_id = Column(Integer, ForeignKey("requisition_header.req_id", ondelete="CASCADE"), nullable=False)
    line_no = Column(Integer, nullable=False)
    
    item_id = Column(Integer)
    item_no = Column(String(100))
    description = Column(String(500))
    uom = Column(String(50))
    qty_requested = Column(Numeric(18, 2), nullable=False, default=0)
    qty_predicted = Column(Numeric(18, 2), default=0)
    qty_issued = Column(Numeric(18, 2), default=0)
    # qty_balance is a generated column - handled by database, not SQLAlchemy
    
    last_unit_price = Column(Numeric(18, 2))
    estimated_total_amount = Column(Numeric(18, 2))
    preferred_supplier_id = Column(Integer)
    lead_time_days = Column(Integer)
    expected_delivery_date = Column(Date)
    line_status = Column(String(50), default="DRAFT")
    
    pr_line_id = Column(Integer)
    po_line_id = Column(Integer)
    remarks = Column(String(500))
    attachment_count = Column(Integer, default=0)
    
    created_by = Column(Integer, nullable=False)
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    header = relationship("RequisitionHeader", back_populates="lines")


# ---------------------------------------------------------
# VENDOR MODELS
# ---------------------------------------------------------
class Vendor(Base):
    __tablename__ = "vendors"
    
    vendor_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    email = Column(String, index=True)
    mobile = Column(String(15), index=True)
    company_name = Column(String(255))
    vendor_type = Column(String(50))  # Service, Product, Trading, Mixed
    gst_number = Column(String(50))
    pan_number = Column(String(50))
    contact_person = Column(String(100))
    address_line1 = Column(String(255))
    address_line2 = Column(String(255))
    city = Column(String(100))
    state = Column(String(100))
    country = Column(String(100), default="India")
    postal_code = Column(String(20))
    is_branch = Column(Boolean, default=False)
    parent_vendor_id = Column(Integer, ForeignKey("vendors.vendor_id"), nullable=True)
    status = Column(String(20), default="pending")  # pending, approved, rejected, clarification
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    created_by_a = Column(String(50))
    created_on_a = Column(DateTime, server_default=func.now())
    created_ip_a = Column(String(45))
    modified_by_a = Column(String(50))
    modified_on_a = Column(DateTime)
    modified_ip_a = Column(String(45))
    company_code = Column(String(50))
    tenant_id = Column(Integer, default=1)
    company_id = Column(Integer, default=3)
    vendor_registration_no = Column(String(20))
    vendor_code = Column(String(20), unique=True, index=True)
    
    # Relationships
    banks = relationship("VendorBank", back_populates="vendor", cascade="all, delete-orphan")
    items = relationship(
        "VendorItem",
        back_populates="vendor",
        cascade="all, delete-orphan",
        foreign_keys="VendorItem.vendor_id",
    )
    documents = relationship("VendorDocument", back_populates="vendor", cascade="all, delete-orphan")
    parent = relationship("Vendor", remote_side=[vendor_id], backref="branches")
    user_account = relationship("User", back_populates="vendor", uselist=False, foreign_keys="User.vendor_id")


class VendorBank(Base):
    __tablename__ = "vendor_banks"
    
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey('vendors.vendor_id', ondelete="CASCADE"), nullable=False)
    bank_name = Column(String(255), nullable=False)
    account_number = Column(String(50), nullable=False)
    ifsc_code = Column(String(20), nullable=False)
    branch_name = Column(String(100), nullable=False)
    account_type = Column(String(20), nullable=False)  # 'Savings', 'Current', 'OD'
    is_primary = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    
    vendor = relationship("Vendor", back_populates="banks")


class VendorItem(Base):
    __tablename__ = "vendor_items"
    
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.vendor_id"), nullable=True)
    item_name = Column(String(255))
    item_type = Column(String(50))  # Service, Product
    uom = Column(String(50))  # Unit of Measure
    description = Column(Text)
    created_by = Column(String(50))
    created_at = Column("created_on", DateTime, server_default=func.now())
    created_ip = Column(String(45))
    modified_by = Column(String(50))
    updated_at = Column("modified_on", DateTime)
    modified_ip = Column(String(45))
    tenant_id = Column(String(50))
    company_code = Column(String(50))
    item_id = Column(Integer)
    erp_code = Column(String(20))
    vendor_product_code = Column(String(20))
    barcode = Column(String(100), unique=True)
    product_fingerprint = Column(JSONB)
    brand = Column(String(100), nullable=False)
    manufacturer = Column(String(255), nullable=False)
    product_name = Column(String(255), nullable=False)
    category = Column(String(100), nullable=False)
    consumer_unit = Column(String(20))
    consumer_unit_size = Column(String(50))
    purchase_unit = Column(String(20))
    pack_size = Column(Numeric(10, 2), nullable=False)
    conversion_factor = Column(Numeric(10, 4))
    # Primary / secondary UOM and conversion factors (purchase ↔ stock).
    uom_p = Column(CHAR(3))
    uom_s = Column(CHAR(3))
    conv_f = Column(Numeric(10, 4))
    conv_t = Column(Numeric(10, 4))
    status = Column(String(20), server_default=text("'Active'"), default="Active")
    vendor_code = Column(
        String(20),
        ForeignKey("vendors.vendor_code", onupdate="CASCADE", ondelete="RESTRICT"),
        index=True,
        nullable=True,
    )
    
    vendor = relationship(
        "Vendor",
        back_populates="items",
        foreign_keys=[vendor_id],
    )
    rates = relationship("VendorRate", back_populates="item", cascade="all, delete-orphan")


class VendorItemFingerprint(VectorBase):
    """Stored on the VECTOR_DB connection (separate from the main app DB)."""

    __tablename__ = "vendor_item_fingerprint"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    vendor_item_id = Column(Integer, nullable=False, index=True)
    fingerprint_json = Column(JSONB)
    embedding = Column(Vector(1536))
    embedding_model = Column(String(100))
    created_at = Column("created_on", DateTime, server_default=func.now())


class ItemEmbedding(VectorBase):
    """Catalog product embeddings on VECTOR_DB (public.item_embeddings).

    Populated from rate-contract vendor items; used to resolve stock lines
    that have no product_code / product_id.
    """

    __tablename__ = "item_embeddings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_code = Column(String(100), nullable=False, index=True)
    item_name = Column(String(255), nullable=False)
    vendor_code = Column(String(100), nullable=False, index=True)
    item_description = Column(Text, nullable=False)
    embedding_text = Column(Text, nullable=False)
    embedding = Column(Vector(1536))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MCPVector(VectorBase):
    """MCP resources and actions stored on the VECTOR_DB connection."""

    __tablename__ = "mcp_vectors"
    __table_args__ = (
        CheckConstraint(
            "mcp_type IN ('faq', 'action', 'knowledge')",
            name="mcp_vectors_type_check",
        ),
    )

    mcp_vector_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    mcp_id = Column(String(100), nullable=False, index=True)
    mcp_type = Column(String(30), nullable=False, index=True)
    sample_query = Column(Text, nullable=False)
    description = Column(Text)
    natural_language_description = Column(Text)
    embedding = Column(Vector(768), nullable=False)
    tenant_id = Column(Integer, nullable=False, index=True)
    company_id = Column(Integer, nullable=False, index=True)
    api_name = Column(String(150))
    api_endpoint = Column(Text)
    http_method = Column(String(10))
    execution_type = Column(String(50))
    request_schema = Column(JSONB)
    required_params = Column(JSONB)
    response_type = Column(String(20))
    created_at = Column(DateTime, server_default=func.now())


class DocumentItemEmbedding(Base):
    __tablename__ = "document_item_embeddings"

    embedding_id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer)
    company_id = Column(Integer)
    source_document_type = Column(String(50))
    source_document_no = Column(String(50))
    source_line_no = Column(Integer)
    item_id = Column(Integer)
    fingerprint = Column(JSONB)
    embedding_text = Column(Text)
    embedding = Column(Vector(1536))
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class VendorRate(Base):
    __tablename__ = "vendor_rates"
    
    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("vendor_items.id", ondelete="CASCADE"), nullable=False)
    vendor_id = Column(
        Integer,
        ForeignKey("vendors.vendor_id", ondelete="CASCADE"),
        nullable=True,
    )
    rate = Column(Numeric(12, 2))
    currency = Column(String(10), default="INR")
    valid_from = Column(Date)
    valid_to = Column(Date)
    status = Column(String(20), default="pending")  # pending, approved, rejected
    created_at = Column(DateTime, server_default=func.now())
    
    item = relationship("VendorItem", back_populates="rates")


class VendorDocument(Base):
    __tablename__ = "vendor_documents"
    
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.vendor_id", ondelete="CASCADE"), nullable=False)
    document_type = Column(String(100))  # GST, PAN, Registration, MSME, Other
    document_number = Column(String(100))
    issue_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)
    document_path = Column(String(255))
    status = Column(String(20), default="pending")  # pending, approved, rejected
    uploaded_at = Column(DateTime, server_default=func.now())
    
    vendor = relationship("Vendor", back_populates="documents")





class MediaAttachment(Base):
    __tablename__ = "media_attachments"

    media_attachment_id = Column(Integer, primary_key=True, index=True)

    media_name = Column(Text, nullable=False)
    media_type = Column(Text, nullable=False)
    media_url = Column(Text, nullable=False)

    document_type_id = Column(Integer, nullable=True)
    context_registry_id = Column(Integer, nullable=False)

    ocr_raw_text = Column(JSONB, nullable=True)
    cleaned_text = Column(JSONB, nullable=True)

    tenant_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)

    effective_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)

    created_by = Column(String(50), nullable=True)
    updated_by = Column(String(50), nullable=True)

    createdAt = Column(TIMESTAMP, server_default=func.now(), nullable=False)
    updatedAt = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now(), nullable=False)






class RFQHeader(Base):
    __tablename__ = "rfq_header"

    # ✅ Composite Primary Key
    tenant_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, primary_key=True)
    document_type = Column(String(100), primary_key=True)
    rfq_no = Column(String(30), primary_key=True)
    version_no = Column(Integer, primary_key=True)

    # ✅ Other Columns
    rfq_id = Column(Integer, unique=True, index=True)
    rfq_date = Column(Date, nullable=False)

    department_id = Column(Integer)
    requester_id = Column(Integer, nullable=False)

    status = Column(String(50))
    priority = Column(String(50))
    source_type = Column(String(100))
    remarks = Column(String(500))

    approved_by = Column(Integer)
    approved_at = Column(DateTime(timezone=True))

    rejected_by = Column(Integer)
    rejected_at = Column(DateTime(timezone=True))
    rejection_reason = Column(String(500))

    created_by = Column(Integer, nullable=False)
    updated_by = Column(Integer)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    document_number = Column(String(200))

    current_editor = Column(String(20), default="BUYER")

    # ✅ Relationships
    lines = relationship(
        "RFQLine",
        back_populates="header",
        cascade="all, delete-orphan"
    )

    vendors = relationship(
        "RFQVendor",
        back_populates="header",
        cascade="all, delete-orphan"
    )



class RFQLine(Base):
    __tablename__ = "rfq_line"

    # ✅ Composite Primary Key
    tenant_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, primary_key=True)
    document_type = Column(String(100), primary_key=True)
    rfq_no = Column(String(30), primary_key=True)
    version_no = Column(Integer, primary_key=True)
    line_no = Column(Integer, primary_key=True)

    # ✅ Columns matching DB
    item_id = Column(Integer)
    item_description = Column(String(500))
    uom = Column(String(50))

    quantity = Column(Numeric(18, 4), nullable=False)
    estimated_rate = Column(Numeric(18, 4))
    estimated_amount = Column(Numeric(18, 4))

    required_date = Column(Date)
    remarks = Column(String(500))

    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_by = Column(Integer)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # ✅ Composite Foreign Key
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "document_type", "rfq_no", "version_no"],
            [
                "rfq_header.tenant_id",
                "rfq_header.company_id",
                "rfq_header.document_type",
                "rfq_header.rfq_no",
                "rfq_header.version_no",
            ],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )

    # ✅ Relationship back
    header = relationship("RFQHeader", back_populates="lines")

class RFQVendor(Base):
    __tablename__ = "rfq_vendor"

    # ✅ Composite Primary Key
    tenant_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, primary_key=True)
    document_type = Column(String(100), primary_key=True)
    rfq_no = Column(String(30), primary_key=True)
    version_no = Column(Integer, primary_key=True)
    vendor_id = Column(Integer, primary_key=True)

    status = Column(String(50))
    current_editor = Column(String(20), default="BUYER")

    # ✅ Audit Columns
    created_by = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    updated_by = Column(Integer)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    # ✅ Composite Foreign Key
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "document_type", "rfq_no", "version_no"],
            [
                "rfq_header.tenant_id",
                "rfq_header.company_id",
                "rfq_header.document_type",
                "rfq_header.rfq_no",
                "rfq_header.version_no",
            ],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
    )

    # ✅ Relationship back
    header = relationship("RFQHeader", back_populates="vendors")


class PurchaseOrderHeader(Base):
    __tablename__ = "purchase_order_header"

    tenant_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, primary_key=True)
    document_type = Column(String(100), primary_key=True)
    po_number = Column(String(30), primary_key=True)
    version_no = Column(Integer, primary_key=True, default=1)

    po_date = Column(Date, nullable=False)
    vendor_id = Column(
        Integer,
        ForeignKey("vendors.vendor_id", ondelete="RESTRICT"),
        nullable=False,
    )
    vendor_site_id = Column(Integer)
    vendor_name_snapshot = Column(String(200))
    currency_code = Column(String(10))
    exchange_rate = Column(Numeric(18, 6))
    incoterms = Column(String(100))
    delivery_terms = Column(String(200))
    billing_address_id = Column(Integer)
    shipping_address_id = Column(Integer)
    expected_delivery_date = Column(Date)
    status = Column(String(50))
    approval_status = Column(String(50), default="DRAFT")
    approved_by = Column(Integer)
    approved_at = Column(DateTime(timezone=True))
    rejected_by = Column(Integer)
    rejected_at = Column(DateTime(timezone=True))
    rejection_reason = Column(String(500))
    po_total_amount = Column(Numeric(18, 2))
    tax_total_amount = Column(Numeric(18, 2))
    discount_amount = Column(Numeric(18, 2))
    net_total_amount = Column(Numeric(18, 2))
    source_type = Column(String(100))
    source_requisition_no = Column(String(30))
    vendor_acknowledged = Column(Boolean, default=False)
    acknowledged_at = Column(DateTime(timezone=True))
    dispatch_status = Column(String(50))
    dispatched_at = Column(DateTime(timezone=True))
    is_payment_terms_overridden = Column(Boolean, default=False)
    remarks = Column(String(500))
    attachment_count = Column(Integer)
    created_by = Column(Integer)
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    document_number = Column(String(200))
    po_id = Column(
        Integer,
        unique=True,
        nullable=False,
        server_default=text("nextval('purchase_order_header_po_id_seq'::regclass)"),
    )
    document_hash = Column(String(64), index=True)
    business_hash = Column(String(64))
    revision_no = Column(Integer, default=1)
    current_revision = Column(Boolean, default=True)
    attachment_path = Column(String(500))
    current_editor = Column(String(255), default="BUYER")

    po_lines = relationship("PurchaseOrderLine", back_populates="header")


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_line"

    tenant_id = Column(Integer, primary_key=True)
    company_id = Column(Integer, primary_key=True)
    document_type = Column(String(100), primary_key=True)
    po_number = Column(String(30), primary_key=True)
    version_no = Column(Integer, primary_key=True)
    line_no = Column(Integer, primary_key=True)

    line_id = Column(
        Integer,
        unique=True,
        nullable=False,
        server_default=text("nextval('purchase_order_line_line_id_seq'::regclass)"),
    )
    po_id = Column(Integer, nullable=False)
    item_id = Column(Integer)
    item_no = Column(String(100))
    description = Column(String(500))
    uom = Column(String(50))
    product_fingerprint = Column(JSONB)
    qty_ordered = Column(Numeric(18, 2))
    suggested_qty = Column(Numeric(18, 2))
    buyer_qty = Column(Numeric(18, 2))
    supplier_qty = Column(Numeric(18, 2))
    qty_received = Column(Numeric(18, 2), default=0)
    qty_invoiced = Column(Numeric(18, 2), default=0)
    unit_price = Column(Numeric(18, 2))
    po_item_rate = Column(Numeric(18, 2))
    rate_contract_item_rate = Column(Numeric(18, 2))
    tax_percent = Column(Numeric(5, 2))
    tax_amount = Column(Numeric(18, 2))
    discount_percent = Column(Numeric(5, 2))
    discount_amount = Column(Numeric(18, 2))
    total_amount = Column(Numeric(18, 2))
    delivery_date = Column(Date)
    lead_time_days = Column(Integer)
    warehouse_id = Column(Integer)
    pr_line_id = Column(Integer, ForeignKey("requisition_line.line_id"))
    pr_requisition_no = Column(String(30))
    pr_version_no = Column(Integer)
    pr_line_no = Column(Integer)
    line_status = Column(String(50))
    remarks = Column(String(500))
    attachment_count = Column(Integer)
    created_by = Column(Integer)
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "company_id", "document_type", "po_number", "version_no"],
            [
                "purchase_order_header.tenant_id",
                "purchase_order_header.company_id",
                "purchase_order_header.document_type",
                "purchase_order_header.po_number",
                "purchase_order_header.version_no",
            ],
        ),
    )

    header = relationship("PurchaseOrderHeader", back_populates="po_lines")


class PurchaseOrderHeaderHistory(Base):
    __tablename__ = "purchase_order_header_h"

    history_id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    document_type = Column(String(100), nullable=False)
    po_number = Column(String(30), nullable=False)
    version_no = Column(Integer, nullable=False, default=1)
    po_date = Column(Date, nullable=False)
    vendor_id = Column(Integer, nullable=False)
    vendor_site_id = Column(Integer)
    vendor_name_snapshot = Column(String(200))
    currency_code = Column(String(10))
    exchange_rate = Column(Numeric(18, 6))
    incoterms = Column(String(100))
    delivery_terms = Column(String(200))
    billing_address_id = Column(Integer)
    shipping_address_id = Column(Integer)
    expected_delivery_date = Column(Date)
    status = Column(String(50))
    approval_status = Column(String(50))
    approved_by = Column(Integer)
    approved_at = Column(DateTime(timezone=True))
    rejected_by = Column(Integer)
    rejected_at = Column(DateTime(timezone=True))
    rejection_reason = Column(String(500))
    po_total_amount = Column(Numeric(18, 2))
    tax_total_amount = Column(Numeric(18, 2))
    discount_amount = Column(Numeric(18, 2))
    net_total_amount = Column(Numeric(18, 2))
    source_type = Column(String(100))
    source_requisition_no = Column(String(30))
    vendor_acknowledged = Column(Boolean, default=False)
    acknowledged_at = Column(DateTime(timezone=True))
    dispatch_status = Column(String(50))
    dispatched_at = Column(DateTime(timezone=True))
    is_payment_terms_overridden = Column(Boolean, default=False)
    remarks = Column(String(500))
    attachment_count = Column(Integer)
    created_by = Column(Integer)
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))
    document_number = Column(String(200))
    po_id = Column(Integer)
    document_hash = Column(String(64))
    business_hash = Column(String(64))
    revision_no = Column(Integer, default=1)
    current_revision = Column(Boolean, default=True)
    history_action = Column(String(50))
    history_reason = Column(String(500))
    history_created_by = Column(Integer)
    history_created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class PurchaseOrderLineHistory(Base):
    __tablename__ = "purchase_order_line_h"

    history_id = Column(BigInteger, primary_key=True, autoincrement=True)
    line_id = Column(Integer)
    tenant_id = Column(Integer, nullable=False)
    company_id = Column(Integer, nullable=False)
    document_type = Column(String(100), nullable=False)
    po_number = Column(String(30), nullable=False)
    version_no = Column(Integer, nullable=False)
    line_no = Column(Integer, nullable=False)
    po_id = Column(Integer, nullable=False)
    item_id = Column(Integer)
    item_no = Column(String(100))
    description = Column(String(500))
    uom = Column(String(50))
    qty_ordered = Column(Numeric(18, 2))
    qty_received = Column(Numeric(18, 2), default=0)
    qty_invoiced = Column(Numeric(18, 2), default=0)
    unit_price = Column(Numeric(18, 2))
    po_item_rate = Column(Numeric(18, 2))
    rate_contract_item_rate = Column(Numeric(18, 2))
    tax_percent = Column(Numeric(5, 2))
    tax_amount = Column(Numeric(18, 2))
    discount_percent = Column(Numeric(5, 2))
    discount_amount = Column(Numeric(18, 2))
    total_amount = Column(Numeric(18, 2))
    delivery_date = Column(Date)
    lead_time_days = Column(Integer)
    warehouse_id = Column(Integer)
    pr_line_id = Column(Integer)
    pr_requisition_no = Column(String(30))
    pr_version_no = Column(Integer)
    pr_line_no = Column(Integer)
    line_status = Column(String(50))
    remarks = Column(String(500))
    attachment_count = Column(Integer)
    created_by = Column(Integer)
    updated_by = Column(Integer)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))
    revision_no = Column(Integer, default=1)
    history_action = Column(String(50))
    history_reason = Column(String(500))
    history_created_by = Column(Integer)
    history_created_at = Column(
        DateTime(timezone=True), server_default=func.now()
    )


class Stock(Base):
    __tablename__ = "stock"

    stock_id = Column(Integer, primary_key=True, autoincrement=True)
    product_code = Column(String(50), nullable=False, unique=True, index=True)
    barcode = Column(String(50), unique=True, index=True)
    product_name = Column(String(255), nullable=False)
    category = Column(String(100), index=True)
    brand = Column(String(100), index=True)
    unit = Column(String(20))
    available_stock = Column(Numeric(18, 3), default=0)
    reorder_level = Column(Numeric(18, 3), default=0)
    reorder_qty = Column(Numeric(18, 3), default=0)
    last_week_sales = Column(Numeric(18, 3), default=0)
    weekly_trend = Column(Numeric(6, 2), default=0)
    suggested_qty = Column(Numeric(18, 3), default=0)
    preferred_supplier = Column(String(150))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    tenant_id = Column(Integer)
    company_id = Column(Integer)
    created_by = Column(Integer)
    updated_by = Column(Integer)


class StockUploadHeader(Base):
    __tablename__ = "stock_upload_header"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "company_id",
            "document_no",
            name="uq_stock_upload_document_no",
        ),
    )

    stock_upload_hdr_id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    company_id = Column(Integer, nullable=False, index=True)
    document_no = Column(String(30), nullable=False)
    upload_datetime = Column(DateTime, server_default=func.now(), index=True)
    upload_source = Column(String(20))
    status = Column(String(20), server_default=text("'PENDING'"), index=True)
    created_by = Column(Integer)
    created_on = Column(DateTime, server_default=func.now())
    updated_by = Column(Integer)
    updated_on = Column(DateTime, server_default=func.now(), onupdate=func.now())


class StockUploadDetail(Base):
    __tablename__ = "stock_upload_detail"
    __table_args__ = (
        UniqueConstraint(
            "stock_upload_hdr_id",
            "line_no",
            name="uq_stock_upload_detail_line",
        ),
    )

    stock_upload_dtl_id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(Integer, nullable=False, index=True)
    company_id = Column(Integer, nullable=False, index=True)
    stock_upload_hdr_id = Column(
        Integer,
        ForeignKey("stock_upload_header.stock_upload_hdr_id", onupdate="CASCADE", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_no = Column(Integer, nullable=False)
    supplier_product_id = Column(Integer, nullable=False, index=True)
    available_stock = Column(Numeric(18, 3), default=0)
    created_by = Column(Integer)
    created_on = Column(DateTime, server_default=func.now())
    updated_by = Column(Integer)
    updated_on = Column(DateTime, server_default=func.now(), onupdate=func.now())


class SalesTransaction(Base):
    __tablename__ = "sales_transactions"
    __table_args__ = (
        Index("idx_sales_transaction_product_date", "product_id", "transaction_date"),
    )

    sales_transaction_id = Column(BigInteger, primary_key=True, autoincrement=True)
    transaction_date = Column(Date, nullable=False, index=True)
    product_id = Column(
        Integer,
        ForeignKey("products.product_id"),
        nullable=False,
        index=True,
    )
    product_name = Column(String(255), nullable=False)
    category = Column(String(100), index=True)
    qty_sold = Column(Numeric(12, 2), nullable=False)
    tenant_id = Column(Integer, server_default=text("1"), index=True)
    company_id = Column(Integer, server_default=text("3"), index=True)
    created_by = Column(Integer)
    created_on = Column(DateTime, server_default=func.now())
    created_ip = Column(String(45))
    modified_by = Column(Integer)
    modified_on = Column(DateTime)
    modified_ip = Column(String(45))

    product = relationship("Product")