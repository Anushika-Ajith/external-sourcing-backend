#!/usr/bin/env python3
"""
Create the immudb database used by the app (default: mydb).

Requires: pip install immudb-py python-dotenv

Usage (from backend/):
  python3 scripts/create_immudb_database.py

Uses IMMUDB_* from .env if present; otherwise localhost:3322, immudb/immudb, database mydb.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# backend/.env
_backend_root = Path(__file__).resolve().parent.parent
load_dotenv(_backend_root / ".env")

try:
    from immudb import ImmudbClient
except ImportError:
    print("Install the client: pip install immudb-py", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    host = os.getenv("IMMUDB_HOST", "localhost")
    port = os.getenv("IMMUDB_PORT", "3322")
    user = os.getenv("IMMUDB_USERNAME", "immudb")
    password = os.getenv("IMMUDB_PASSWORD", "immudb")
    db_name = os.getenv("IMMUDB_DATABASE", "mydb")
    db_bytes = db_name.encode("utf-8")

    addr = f"{host}:{port}"
    client = ImmudbClient(addr)
    client.login(user, password)

    try:
        client.createDatabase(db_bytes)
        print(f"Created database '{db_name}' on {addr}.")
    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ("already", "exist", "duplicate")):
            print(f"Database '{db_name}' already exists on {addr} (nothing to do).")
        else:
            client.logout()
            raise

    client.useDatabase(db_bytes)
    client.logout()
    print("Done.")


if __name__ == "__main__":
    main()
