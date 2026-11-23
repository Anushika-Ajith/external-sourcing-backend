"""Tests for central API error handling."""

from __future__ import annotations

import unittest

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError, OperationalError

from services.error_handling import (
    GENERIC_SERVER_MESSAGE,
    install_error_handling,
    looks_technical,
)


class _Body(BaseModel):
    vendor_name: str
    qty: int


def _build_app() -> FastAPI:
    app = FastAPI()
    install_error_handling(app)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"], allow_methods=["*"], allow_headers=["*"])

    @app.post("/validate")
    def validate(body: _Body):
        return body

    @app.get("/not-found")
    def not_found():
        raise HTTPException(status_code=404, detail="Vendor 12 was not found.")

    @app.get("/leaky-500")
    def leaky_500():
        raise HTTPException(status_code=500, detail="Error updating RFQ header: (psycopg2.errors.UndefinedColumn) boom")

    @app.get("/leaky-400")
    def leaky_400():
        raise HTTPException(
            status_code=400,
            detail='Database constraint violation: duplicate key value violates unique constraint "x"',
        )

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret internal state /srv/app/foo.py")

    @app.get("/integrity")
    def integrity():
        class _Orig(Exception):
            pass

        raise IntegrityError(
            "INSERT INTO t ...",
            {},
            _Orig('duplicate key value violates unique constraint "t_pkey"\nDETAIL:  Key (vendor_code)=(V1) already exists.'),
        )

    @app.get("/db-down")
    def db_down():
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    @app.get("/upstream")
    def upstream():
        import requests

        raise requests.exceptions.ConnectionError("HTTPConnectionPool(host='ocr', port=80): Max retries exceeded")

    return app


class TestErrorHandling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(_build_app(), raise_server_exceptions=False)

    def _origin(self):
        return {"Origin": "http://localhost:3000"}

    def test_validation_error_is_friendly_and_keeps_detail_list(self):
        res = self.client.post("/validate", json={"qty": "abc"})
        self.assertEqual(res.status_code, 422)
        body = res.json()
        self.assertIsInstance(body["detail"], list)
        self.assertIn("Vendor name is required", body["message"])
        self.assertEqual(body["code"], "validation_error")
        self.assertNotIn("body", body)

    def test_http_exception_passthrough(self):
        res = self.client.get("/not-found")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json()["message"], "Vendor 12 was not found.")
        self.assertEqual(res.json()["detail"], "Vendor 12 was not found.")

    def test_unknown_route_has_friendly_message(self):
        res = self.client.get("/does-not-exist")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(res.json()["message"])

    def test_technical_5xx_detail_is_hidden(self):
        res = self.client.get("/leaky-500")
        self.assertEqual(res.status_code, 500)
        message = res.json()["message"]
        self.assertEqual(message, "Error updating RFQ header. Please try again.")
        self.assertNotIn("psycopg2", res.text)

    def test_technical_4xx_detail_is_translated(self):
        res = self.client.get("/leaky-400")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["message"], "A record with the same details already exists.")

    def test_unhandled_exception_returns_json_with_cors(self):
        res = self.client.get("/boom", headers=self._origin())
        self.assertEqual(res.status_code, 500)
        body = res.json()
        self.assertEqual(body["message"], GENERIC_SERVER_MESSAGE)
        self.assertEqual(body["code"], "internal_error")
        self.assertTrue(body["error_id"])
        self.assertNotIn("secret", res.text)
        self.assertEqual(res.headers.get("access-control-allow-origin"), "http://localhost:3000")

    def test_integrity_error_maps_to_conflict(self):
        res = self.client.get("/integrity", headers=self._origin())
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual(body["code"], "duplicate_record")
        self.assertIn("Vendor code", body["message"])
        self.assertNotIn("INSERT", res.text)

    def test_database_down_maps_to_503(self):
        res = self.client.get("/db-down")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "database_unavailable")

    def test_upstream_failure_maps_to_502(self):
        res = self.client.get("/upstream", headers=self._origin())
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json()["code"], "upstream_unavailable")
        self.assertNotIn("HTTPConnectionPool", res.text)

    def test_looks_technical(self):
        self.assertTrue(looks_technical("(psycopg2.errors.X) bad"))
        self.assertFalse(looks_technical("Vendor not found"))


if __name__ == "__main__":
    unittest.main()
