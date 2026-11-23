import json
import logging
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)


def insert_into_immudb(new_media, raw_text):
    """
    Append OCR payload to immudb ledger. Does not raise: failures are logged only
    so vendor OCR (and other callers) still return 200 when immudb is unreachable.
    """
    if not settings.immudb_enabled:
        logger.debug("immudb disabled (IMMUDB_ENABLED), skip ledger insert")
        return

    # Lazy import: immudb/appier parses ./.env on import and crashes on multiline PEMs.
    try:
        import grpc
        from immudb import ImmudbClient
    except Exception as e:
        logger.warning("immudb client unavailable, ledger not written: %s", e)
        return

    addr = f"{settings.immudb_host}:{settings.immudb_port}"
    db_name = settings.immudb_database.encode("utf-8")

    insert_sql = """
    INSERT INTO media_attachment_ledger (
        media_name,
        media_type,
        media_url,
        context_registry_id,
        ocr_raw_text,
        tenant_id,
        company_id,
        created_by,
        created_at
    )
    VALUES (
        @media_name,
        @media_type,
        @media_url,
        @context_registry_id,
        @ocr_raw_text,
        @tenant_id,
        @company_id,
        @created_by,
        @created_at
    )
    """

    params = {
        "media_name": new_media.media_name,
        "media_type": new_media.media_type,
        "media_url": new_media.media_url,
        "context_registry_id": new_media.context_registry_id,
        "ocr_raw_text": json.dumps(raw_text),
        "tenant_id": new_media.tenant_id,
        "company_id": new_media.company_id,
        "created_by": new_media.created_by,
        "created_at": datetime.utcnow(),
    }

    try:
        client = ImmudbClient(addr)
        client.login(settings.immudb_username, settings.immudb_password)
        client.useDatabase(db_name)
        client.sqlExec(insert_sql, params)
        mid = getattr(new_media, "id", None)
        logger.info(
            "[immudb] Inserted media_attachment_ledger id=%s name=%r",
            mid,
            new_media.media_name,
        )
        client.logout()
    except grpc.RpcError as e:
        logger.warning(
            "immudb unavailable, ledger not written (%s): %s",
            addr,
            e.details() if hasattr(e, "details") else e,
        )
    except Exception as e:
        logger.warning("immudb error, ledger not written: %s", e)
