# backend/database.py

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from config import settings  # ✅ Import the settings instance

# Main database
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Vector / pgvector database (vendor_item_fingerprint lives here)
vector_engine = create_engine(
    settings.vector_database_url, pool_pre_ping=True, pool_recycle=1800
)
VectorSessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=vector_engine
)
VectorBase = declarative_base()

# ERP Base (kept for erp_models.py if needed, but ERP database connection is removed - using NestJS API instead)
ErpBase = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_vector_db():
    db = VectorSessionLocal()
    try:
        yield db
    finally:
        db.close()
