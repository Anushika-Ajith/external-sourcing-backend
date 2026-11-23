# RFQ Create API - cURL Examples

## Endpoints
- `POST /rfq` - Create RFQ (original endpoint)
- `POST /rfq/complete` - Create complete RFQ (explicit endpoint)

Both endpoints accept the same payload structure.

**Note:** No authentication required - these endpoints do not require bearer tokens.

## Base URL
Assuming the API runs on `http://localhost:8000` (adjust as needed)

---

## Example 1: Complete RFQ with Header, Lines, and Vendors

```bash
curl -X POST "http://localhost:8000/rfq/complete" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": 1,
    "company_id": 1,
    "document_type": "RFQ",
    "rfq_no": "RFQ-2024-001",
    "version_no": 1,
    "rfq_date": "2024-01-15",
    "department_id": 10,
    "requester_id": 5,
    "status": "DRAFT",
    "priority": "HIGH",
    "source_type": "MANUAL",
    "remarks": "Urgent procurement request",
    "document_number": "DOC-RFQ-2024-001",
    "created_by": 1,
    "lines": [
      {
        "line_no": 1,
        "item_id": 100,
        "item_description": "Medical Gloves - Latex Free",
        "uom": "BOX",
        "quantity": 50.0000,
        "estimated_rate": 25.50,
        "estimated_amount": 1275.00,
        "required_date": "2024-02-01",
        "remarks": "Size: Medium"
      },
      {
        "line_no": 2,
        "item_id": 101,
        "item_description": "Surgical Masks N95",
        "uom": "PACK",
        "quantity": 100.0000,
        "estimated_rate": 15.75,
        "estimated_amount": 1575.00,
        "required_date": "2024-02-01",
        "remarks": "FDA approved"
      },
      {
        "line_no": 3,
        "item_id": null,
        "item_description": "Hand Sanitizer 500ml",
        "uom": "BOTTLE",
        "quantity": 200.0000,
        "estimated_rate": 8.25,
        "estimated_amount": 1650.00,
        "required_date": "2024-02-05",
        "remarks": null
      }
    ],
    "vendors": [
      {
        "vendor_id": 1
      },
      {
        "vendor_id": 2
      },
      {
        "vendor_id": 3
      }
    ]
  }'
```

---

## Example 2: Minimal RFQ (Header Only, No Lines/Vendors)

```bash
curl -X POST "http://localhost:8000/rfq" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": 1,
    "company_id": 1,
    "document_type": "RFQ",
    "rfq_no": "RFQ-2024-002",
    "version_no": 1,
    "rfq_date": "2024-01-16",
    "requester_id": 5,
    "status": "DRAFT"
  }'
```

---

## Example 3: RFQ with Lines Only (No Vendors)

```bash
curl -X POST "http://localhost:8000/rfq/complete" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": 1,
    "company_id": 1,
    "document_type": "RFQ",
    "rfq_no": "RFQ-2024-003",
    "version_no": 1,
    "rfq_date": "2024-01-17",
    "department_id": 10,
    "requester_id": 5,
    "status": "DRAFT",
    "priority": "MEDIUM",
    "lines": [
      {
        "line_no": 1,
        "item_description": "Disposable Syringes 5ml",
        "uom": "PIECE",
        "quantity": 500.0000,
        "estimated_rate": 2.50,
        "estimated_amount": 1250.00,
        "required_date": "2024-02-10"
      }
    ]
  }'
```

---

## Example 4: RFQ with Vendors Only (No Lines)

```bash
curl -X POST "http://localhost:8000/rfq" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": 1,
    "company_id": 1,
    "document_type": "RFQ",
    "rfq_no": "RFQ-2024-004",
    "version_no": 1,
    "rfq_date": "2024-01-18",
    "requester_id": 5,
    "status": "DRAFT",
    "vendors": [
      {
        "vendor_id": 1
      },
      {
        "vendor_id": 5
      }
    ]
  }'
```

---

## Example 5: Using created_by Default (Omit created_by field)

```bash
curl -X POST "http://localhost:8000/rfq/complete" \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": 1,
    "company_id": 1,
    "document_type": "RFQ",
    "rfq_no": "RFQ-2024-005",
    "version_no": 1,
    "rfq_date": "2024-01-19",
    "requester_id": 5,
    "status": "DRAFT",
    "priority": "HIGH",
    "lines": [
      {
        "line_no": 1,
        "item_description": "Thermometer Digital",
        "uom": "PIECE",
        "quantity": 20.0000,
        "estimated_rate": 12.00,
        "required_date": "2024-02-15"
      }
    ],
    "vendors": [
      {
        "vendor_id": 2
      }
    ]
  }'
```

**Note:** When `created_by` is omitted, it will automatically use the current authenticated user's ID.

---

## Pretty Formatted JSON (for easier reading)

Save this as `rfq_payload.json`:

```json
{
  "tenant_id": 1,
  "company_id": 1,
  "document_type": "RFQ",
  "rfq_no": "RFQ-2024-001",
  "version_no": 1,
  "rfq_date": "2024-01-15",
  "department_id": 10,
  "requester_id": 5,
  "status": "DRAFT",
  "priority": "HIGH",
  "source_type": "MANUAL",
  "remarks": "Urgent procurement request",
  "document_number": "DOC-RFQ-2024-001",
  "lines": [
    {
      "line_no": 1,
      "item_id": 100,
      "item_description": "Medical Gloves - Latex Free",
      "uom": "BOX",
      "quantity": 50.0000,
      "estimated_rate": 25.50,
      "estimated_amount": 1275.00,
      "required_date": "2024-02-01",
      "remarks": "Size: Medium"
    },
    {
      "line_no": 2,
      "item_id": 101,
      "item_description": "Surgical Masks N95",
      "uom": "PACK",
      "quantity": 100.0000,
      "estimated_rate": 15.75,
      "estimated_amount": 1575.00,
      "required_date": "2024-02-01",
      "remarks": "FDA approved"
    }
  ],
  "vendors": [
    {
      "vendor_id": 1
    },
    {
      "vendor_id": 2
    }
  ]
}
```

Then use:

```bash
curl -X POST "http://localhost:8000/rfq/complete" \
  -H "Content-Type: application/json" \
  -d @rfq_payload.json
```

---

## Response Example

Successful response (HTTP 201):

```json
{
  "tenant_id": 1,
  "company_id": 1,
  "document_type": "RFQ",
  "rfq_no": "RFQ-2024-001",
  "version_no": 1,
  "rfq_date": "2024-01-15",
  "department_id": 10,
  "requester_id": 5,
  "status": "DRAFT",
  "priority": "HIGH",
  "source_type": "MANUAL",
  "remarks": "Urgent procurement request",
  "approved_by": null,
  "approved_at": null,
  "rejected_by": null,
  "rejected_at": null,
  "rejection_reason": null,
  "document_number": "DOC-RFQ-2024-001",
  "rfq_id": 123,
  "created_by": 1,
  "updated_by": null,
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z",
  "lines": [
    {
      "line_no": 1,
      "item_id": 100,
      "item_description": "Medical Gloves - Latex Free",
      "uom": "BOX",
      "quantity": 50.0000,
      "estimated_rate": 25.50,
      "estimated_amount": 1275.00,
      "required_date": "2024-02-01",
      "remarks": "Size: Medium",
      "tenant_id": 1,
      "company_id": 1,
      "document_type": "RFQ",
      "rfq_no": "RFQ-2024-001",
      "version_no": 1,
      "created_by": 1,
      "updated_by": null,
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-01-15T10:30:00Z"
    }
  ],
  "vendors": [
    {
      "vendor_id": 1,
      "tenant_id": 1,
      "company_id": 1,
      "document_type": "RFQ",
      "rfq_no": "RFQ-2024-001",
      "version_no": 1,
      "created_by": 1,
      "updated_by": null,
      "created_at": "2024-01-15T10:30:00Z",
      "updated_at": "2024-01-15T10:30:00Z"
    }
  ]
}
```

---

## Error Response Examples

### Duplicate RFQ (HTTP 400):
```json
{
  "detail": "RFQ header with this composite key already exists"
}
```

### Validation Error (HTTP 422):
```json
{
  "detail": [
    {
      "loc": ["body", "rfq_date"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ]
}
```

### Unauthorized (HTTP 401):
```json
{
  "detail": "Not authenticated"
}
```

---

## Notes

1. **No Authentication Required**: These endpoints do not require bearer tokens or authentication headers
2. **created_by**: **REQUIRED** field - must be provided in the payload (user ID who is creating the RFQ)
3. **Date Format**: Use ISO 8601 format (YYYY-MM-DD) for dates
4. **Decimal Values**: Use decimal format for quantity, rates, and amounts (e.g., 50.0000)
5. **Transaction**: All operations (header, lines, vendors) are atomic - if any part fails, everything is rolled back
6. **lines and vendors**: Both are optional arrays - you can create RFQ with or without them

