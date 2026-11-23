from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db, get_vector_db
from models import User, VendorItem, VendorItemFingerprint
from schemas.vendor_item_fingerprint import (
    VendorItemFingerprintCreate,
    VendorItemFingerprintResponse,
    VendorItemFingerprintSimilarityResult,
    VendorItemFingerprintSimilaritySearch,
    VendorItemFingerprintUpdate,
)
from services.auth_utils import get_current_user

router = APIRouter(
    prefix="/vendor-item-fingerprints",
    tags=["vendor-item-fingerprints"],
    responses={404: {"description": "Not found"}},
)


def _ensure_vendor_item_access(
    db: Session,
    vendor_item_id: int,
    current_user: User,
) -> VendorItem:
    item = db.query(VendorItem).filter(VendorItem.id == vendor_item_id).first()
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Vendor item not found",
        )
    return item


def _vendor_item_ids_for_user(db: Session, current_user: User) -> List[int]:
    """Optional scope by linked vendor_id; empty list means no vendor filter."""
    if not current_user.vendor_id:
        return []
    return [
        row[0]
        for row in db.query(VendorItem.id)
        .filter(VendorItem.vendor_id == current_user.vendor_id)
        .all()
    ]


def _get_fingerprint_or_404(
    db: Session,
    vector_db: Session,
    fingerprint_id: int,
    current_user: User,
) -> VendorItemFingerprint:
    row = (
        vector_db.query(VendorItemFingerprint)
        .filter(VendorItemFingerprint.id == fingerprint_id)
        .first()
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Fingerprint not found",
        )
    _ensure_vendor_item_access(db, row.vendor_item_id, current_user)
    return row


@router.get("", response_model=List[VendorItemFingerprintResponse])
async def list_vendor_item_fingerprints(
    vendor_item_id: Optional[int] = Query(None),
    embedding_model: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    q = vector_db.query(VendorItemFingerprint)

    if vendor_item_id is not None:
        _ensure_vendor_item_access(db, vendor_item_id, current_user)
        q = q.filter(VendorItemFingerprint.vendor_item_id == vendor_item_id)
    else:
        # Optional linked-vendor filter only (not a role gate).
        allowed_ids = _vendor_item_ids_for_user(db, current_user)
        if allowed_ids:
            q = q.filter(VendorItemFingerprint.vendor_item_id.in_(allowed_ids))

    if embedding_model:
        q = q.filter(VendorItemFingerprint.embedding_model == embedding_model)

    return (
        q.order_by(VendorItemFingerprint.id.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.get("/{fingerprint_id}", response_model=VendorItemFingerprintResponse)
async def get_vendor_item_fingerprint(
    fingerprint_id: int,
    db: Session = Depends(get_db),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    return _get_fingerprint_or_404(db, vector_db, fingerprint_id, current_user)


@router.post(
    "",
    response_model=VendorItemFingerprintResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_vendor_item_fingerprint(
    body: VendorItemFingerprintCreate,
    db: Session = Depends(get_db),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    _ensure_vendor_item_access(db, body.vendor_item_id, current_user)

    row = VendorItemFingerprint(**body.model_dump())
    try:
        vector_db.add(row)
        vector_db.commit()
    except IntegrityError as exc:
        vector_db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not create fingerprint: {exc.orig}",
        )
    vector_db.refresh(row)
    return row


@router.put("/{fingerprint_id}", response_model=VendorItemFingerprintResponse)
async def update_vendor_item_fingerprint(
    fingerprint_id: int,
    body: VendorItemFingerprintUpdate,
    db: Session = Depends(get_db),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_fingerprint_or_404(db, vector_db, fingerprint_id, current_user)
    updates = body.model_dump(exclude_unset=True)

    new_vendor_item_id = updates.pop("vendor_item_id", None)
    if new_vendor_item_id is not None:
        _ensure_vendor_item_access(db, new_vendor_item_id, current_user)
        row.vendor_item_id = new_vendor_item_id

    for key, value in updates.items():
        setattr(row, key, value)

    try:
        vector_db.commit()
    except IntegrityError as exc:
        vector_db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not update fingerprint: {exc.orig}",
        )
    vector_db.refresh(row)
    return row


@router.delete("/{fingerprint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vendor_item_fingerprint(
    fingerprint_id: int,
    db: Session = Depends(get_db),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    row = _get_fingerprint_or_404(db, vector_db, fingerprint_id, current_user)
    vector_db.delete(row)
    vector_db.commit()


@router.post("/search/similar", response_model=List[VendorItemFingerprintSimilarityResult])
async def search_similar_vendor_item_fingerprints(
    body: VendorItemFingerprintSimilaritySearch,
    db: Session = Depends(get_db),
    vector_db: Session = Depends(get_vector_db),
    current_user: User = Depends(get_current_user),
):
    """Find nearest fingerprints by cosine distance (<=> operator) on VECTOR_DB."""
    distance = VendorItemFingerprint.embedding.cosine_distance(body.embedding).label(
        "distance"
    )
    q = vector_db.query(VendorItemFingerprint, distance).filter(
        VendorItemFingerprint.embedding.isnot(None)
    )

    if body.vendor_item_id is not None:
        _ensure_vendor_item_access(db, body.vendor_item_id, current_user)
        q = q.filter(VendorItemFingerprint.vendor_item_id == body.vendor_item_id)
    else:
        # Optional linked-vendor filter only (not a role gate).
        allowed_ids = _vendor_item_ids_for_user(db, current_user)
        if allowed_ids:
            q = q.filter(VendorItemFingerprint.vendor_item_id.in_(allowed_ids))

    if body.embedding_model:
        q = q.filter(VendorItemFingerprint.embedding_model == body.embedding_model)

    rows = q.order_by(distance).limit(body.limit).all()

    return [
        VendorItemFingerprintSimilarityResult(
            id=row.id,
            vendor_item_id=row.vendor_item_id,
            fingerprint_json=row.fingerprint_json,
            embedding=list(row.embedding) if row.embedding is not None else None,
            embedding_model=row.embedding_model,
            created_at=row.created_at,
            distance=float(dist),
        )
        for row, dist in rows
    ]
