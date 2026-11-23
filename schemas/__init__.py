# # This file makes the schemas directory a Python package
# # and makes the schemas available at the package level

# # Import all schemas from the vendor module
# from .vendor import (
#     # Vendor schemas
#     Vendor,
#     VendorCreate,
#     VendorUpdate,
#     VendorBase,
    
#     # Bank schemas
#     VendorBank,
#     VendorBankCreate,
#     VendorBankBase,
    
#     # Item schemas
#     VendorItem,
#     VendorItemCreate,
#     VendorItemBase,
    
#     # Document schemas
#     VendorDocument,
#     VendorDocumentCreate,
#     VendorDocumentBase,
    
#     # Rate schemas
#     VendorRate,
#     VendorRateCreate,
#     VendorRateBase,
    
#     # OTP & Audit
#     OTPVerify,
#     AuditLog,
    
#     # Compound
#     VendorApplication,
# )

# # Export all schemas
# __all__ = [
#     # Vendor
#     'Vendor', 'VendorCreate', 'VendorUpdate', 'VendorBase',
    
#     # Vendor Bank
#     'VendorBank', 'VendorBankCreate', 'VendorBankBase',
    
#     # Vendor Item
#     'VendorItem', 'VendorItemCreate', 'VendorItemBase',
    
#     # Vendor Document
#     'VendorDocument', 'VendorDocumentCreate', 'VendorDocumentBase',
    
#     # Vendor Rate
#     'VendorRate', 'VendorRateCreate', 'VendorRateBase',
    
#     # OTP & Audit
#     'OTPVerify', 'AuditLog',

#     # Compound
#     'VendorApplication',
# ]