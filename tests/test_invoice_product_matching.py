"""Unit tests for invoice product matching engine helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from services.invoice_product_matching_engine import (
    MATCH_TYPE_MANUAL,
    MATCH_TYPE_MULTIPLE,
    PoProductCandidate,
    ProgressiveFingerprintMatch,
    _apply_weak_embedding_fallback,
    _finalize_fingerprint_pool,
    _has_domain_conflict_from_stages,
    progressive_match_fingerprint_to_po_products,
)


BISCUIT_CANDIDATES = [
    PoProductCandidate(
        po_line_no=1,
        item_id=49,
        item_no="SUP00042",
        description="Good Day",
        uom="CARTON",
        product_fingerprint={
            "uom": "CARTON",
            "brand": "GOOD DAY",
            "generic": "GOOD DAY BISCUITS",
            "category": "FMCG",
            "pack_size": "60 x 100 g",
            "manufacturer": "BRITANNIA INDUSTRIES LIMITED",
            "normalized_name": "GOOD DAY BISCUITS",
        },
    ),
    PoProductCandidate(
        po_line_no=2,
        item_id=47,
        item_no="SUP00040",
        description="Britannia",
        uom="CARTON",
        product_fingerprint={
            "uom": "CARTON",
            "brand": "BRITANNIA",
            "generic": "BISCUITS",
            "category": "BISCUITS",
            "pack_size": "60 x 100 g",
            "manufacturer": "BRITANNIA",
            "normalized_name": "BISCUITS",
        },
    ),
]


class TestInvoiceDomainConflict(unittest.TestCase):
    def test_syringe_category_conflict_is_red(self):
        stages = [
            {
                "field": "category",
                "status": "skipped_empty_result",
                "invoice_value": "MEDICAL SUPPLIES",
                "remaining": 2,
            }
        ]
        self.assertTrue(_has_domain_conflict_from_stages(stages))

    def test_marie_gold_brand_mismatch_is_not_domain_conflict(self):
        stages = [
            {
                "field": "brand",
                "status": "skipped_empty_result",
                "invoice_value": "MARIE GOLD",
                "remaining": 2,
            }
        ]
        self.assertFalse(_has_domain_conflict_from_stages(stages))

    def test_weak_embedding_fallback_yellow_for_same_domain(self):
        result = ProgressiveFingerprintMatch()
        ranked = [(BISCUIT_CANDIDATES[0], 0.82), (BISCUIT_CANDIDATES[1], 0.85)]
        _apply_weak_embedding_fallback(
            result,
            all_ranked=ranked,
            filtered_pool_size=2,
            domain_conflict=False,
        )
        self.assertEqual(result.match_type, MATCH_TYPE_MULTIPLE)
        self.assertEqual(len(result.ranked_candidates), 2)
        self.assertEqual(result.details["reason"], "weak_semantic_match")

    def test_weak_embedding_fallback_red_for_domain_conflict(self):
        result = ProgressiveFingerprintMatch()
        ranked = [(BISCUIT_CANDIDATES[0], 0.82)]
        _apply_weak_embedding_fallback(
            result,
            all_ranked=ranked,
            filtered_pool_size=2,
            domain_conflict=True,
        )
        self.assertEqual(result.match_type, MATCH_TYPE_MANUAL)
        self.assertEqual(result.ranked_candidates, [])
        self.assertEqual(result.details["reason"], "no_semantic_match")


class TestInvoiceFinalizeFingerprintPool(unittest.TestCase):
    @patch(
        "services.invoice_product_matching_engine.rank_acceptable_po_candidates_by_embedding"
    )
    def test_marie_gold_weak_embedding_becomes_yellow(self, mock_rank):
        mock_rank.return_value = ([], [(BISCUIT_CANDIDATES[0], 0.82), (BISCUIT_CANDIDATES[1], 0.85)])
        result = ProgressiveFingerprintMatch(
            matched_fields=["uom"],
            details={
                "stages": [
                    {"field": "brand", "status": "skipped_empty_result", "invoice_value": "MARIE GOLD", "remaining": 2},
                    {"field": "uom", "status": "applied", "invoice_value": "CARTON", "remaining": 2},
                ]
            },
        )
        finalized = _finalize_fingerprint_pool(
            result,
            pool=BISCUIT_CANDIDATES,
            all_candidates=BISCUIT_CANDIDATES,
            invoice_fingerprint={"brand": "MARIE GOLD", "uom": "CARTON", "normalized_name": "GOLD"},
            product_name="Marie Gold",
        )
        self.assertEqual(finalized.match_type, MATCH_TYPE_MULTIPLE)
        self.assertEqual(len(finalized.ranked_candidates), 2)

    @patch(
        "services.invoice_product_matching_engine.rank_acceptable_po_candidates_by_embedding"
    )
    def test_syringe_medical_category_becomes_red(self, mock_rank):
        mock_rank.return_value = ([], [(BISCUIT_CANDIDATES[0], 0.82), (BISCUIT_CANDIDATES[1], 0.85)])
        result = ProgressiveFingerprintMatch(
            matched_fields=["uom"],
            details={
                "stages": [
                    {
                        "field": "category",
                        "status": "skipped_empty_result",
                        "invoice_value": "MEDICAL SUPPLIES",
                        "remaining": 2,
                    },
                    {"field": "uom", "status": "applied", "invoice_value": "CARTON", "remaining": 2},
                ]
            },
        )
        finalized = _finalize_fingerprint_pool(
            result,
            pool=BISCUIT_CANDIDATES,
            all_candidates=BISCUIT_CANDIDATES,
            invoice_fingerprint={
                "category": "MEDICAL SUPPLIES",
                "uom": "CARTON",
                "normalized_name": "SYRINGE",
                "generic": "SYRINGE",
            },
            product_name="Syringe",
        )
        self.assertEqual(finalized.match_type, MATCH_TYPE_MANUAL)
        self.assertEqual(finalized.ranked_candidates, [])
        self.assertEqual(finalized.details["reason"], "no_semantic_match")


class TestProgressiveMatchIntegration(unittest.TestCase):
    @patch(
        "services.invoice_product_matching_engine.rank_acceptable_po_candidates_by_embedding"
    )
    def test_progressive_marie_gold_end_to_end(self, mock_rank):
        mock_rank.return_value = ([], [(BISCUIT_CANDIDATES[0], 0.82), (BISCUIT_CANDIDATES[1], 0.85)])
        result = progressive_match_fingerprint_to_po_products(
            {"brand": "MARIE GOLD", "uom": "CARTON", "normalized_name": "GOLD", "generic": "GOLD"},
            BISCUIT_CANDIDATES,
            product_name="Marie Gold",
        )
        self.assertEqual(result.match_type, MATCH_TYPE_MULTIPLE)
        self.assertEqual(len(result.ranked_candidates), 2)

    @patch(
        "services.invoice_product_matching_engine.rank_acceptable_po_candidates_by_embedding"
    )
    def test_progressive_syringe_end_to_end(self, mock_rank):
        mock_rank.return_value = ([], [(BISCUIT_CANDIDATES[0], 0.82), (BISCUIT_CANDIDATES[1], 0.85)])
        result = progressive_match_fingerprint_to_po_products(
            {
                "category": "MEDICAL SUPPLIES",
                "uom": "CARTON",
                "normalized_name": "SYRINGE",
                "generic": "SYRINGE",
            },
            BISCUIT_CANDIDATES,
            product_name="Syringe",
        )
        self.assertEqual(result.match_type, MATCH_TYPE_MANUAL)
        self.assertEqual(result.ranked_candidates, [])


if __name__ == "__main__":
    unittest.main()
