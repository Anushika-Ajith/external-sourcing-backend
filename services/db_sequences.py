"""Keep Postgres ``users.user_id`` serial aligned with real rows.

JWT / Nest provisioning often INSERTs with an explicit Foundation ``user_id``.
That does **not** advance ``users_user_id_seq``, so the next auto-insert
collides (``users_pkey`` duplicate). Avoid by:

1. DB trigger (always-on) after every INSERT
2. ``create_user()`` helper before/after Python inserts
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import User

logger = logging.getLogger(__name__)

_ENSURE_USERS_SEQ_SQL = """
CREATE OR REPLACE FUNCTION public.sync_users_user_id_seq()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  PERFORM setval(
    pg_get_serial_sequence('public.users', 'user_id'),
    (SELECT COALESCE(MAX(user_id), 1) FROM public.users),
    true
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_users_sync_user_id_seq ON public.users;

CREATE TRIGGER trg_users_sync_user_id_seq
AFTER INSERT ON public.users
FOR EACH ROW
EXECUTE PROCEDURE public.sync_users_user_id_seq();
"""


def sync_users_id_sequence(db: Session) -> None:
    """Point ``users_user_id_seq`` at ``MAX(users.user_id)`` (next nextval = max+1)."""
    db.execute(
        text(
            "SELECT setval("
            "  pg_get_serial_sequence('users', 'user_id'),"
            "  GREATEST(COALESCE((SELECT MAX(user_id) FROM users), 1), 1),"
            "  true"
            ")"
        )
    )


def ensure_users_id_sequence_guard(engine) -> None:
    """
    Install AFTER INSERT trigger + one-shot sync.

    Call once at app startup so explicit-id inserts cannot leave the serial behind.
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(_ENSURE_USERS_SEQ_SQL))
            conn.execute(
                text(
                    "SELECT setval("
                    "  pg_get_serial_sequence('users', 'user_id'),"
                    "  GREATEST(COALESCE((SELECT MAX(user_id) FROM users), 1), 1),"
                    "  true"
                    ")"
                )
            )
        logger.info("[db] users.user_id sequence guard installed and synced")
    except Exception as exc:
        logger.warning("[db] Could not install users.user_id sequence guard: %s", exc)


def _is_users_pkey_collision(exc: BaseException) -> bool:
    msg = str(getattr(exc, "orig", exc)).lower()
    return "users_pkey" in msg or (
        "duplicate key" in msg and "user_id" in msg
    )


def create_user(
    db: Session,
    *,
    email: str,
    hashed_password: str,
    role: str,
    is_active: bool = True,
    vendor_id: Optional[int] = None,
    user_id: Optional[int] = None,
    commit: bool = True,
) -> User:
    """
    Insert a local ``users`` row without sequence collisions.

    - Auto id: sync sequence, insert, retry once on ``users_pkey``
    - Explicit id (JWT/Foundation): insert, then sync sequence
    """

    def _build() -> User:
        kwargs = {
            "email": email,
            "hashed_password": hashed_password,
            "role": role,
            "is_active": is_active,
            "vendor_id": vendor_id,
        }
        if user_id is not None:
            kwargs["user_id"] = int(user_id)
        return User(**kwargs)

    if user_id is None:
        sync_users_id_sequence(db)

    user = _build()
    db.add(user)
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        if user_id is not None or not _is_users_pkey_collision(exc):
            raise
        logger.warning(
            "[db] users_pkey collision on create; resyncing sequence and retrying email=%s",
            email,
        )
        sync_users_id_sequence(db)
        user = _build()
        db.add(user)
        if commit:
            db.commit()
        else:
            db.flush()

    if user_id is not None:
        sync_users_id_sequence(db)
        if commit:
            db.commit()

    if commit:
        db.refresh(user)
    return user
