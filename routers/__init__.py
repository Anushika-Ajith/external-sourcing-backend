# Import all routers here
from .auth import router as auth_router
from .otp import router as otp_router
# from .vendor import router as vendor_router

# List of all routers to be included in the main FastAPI app
routers = [
    auth_router,
    otp_router,
    # vendor_router,
]