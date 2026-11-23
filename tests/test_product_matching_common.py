"""Unit tests for shared product matching helpers."""

from __future__ import annotations

import unittest
from unittest import mock

from services.product_matching_common import (
    LINE_STATUS_NO_MATCH,
    LINE_STATUS_PENDING_CONFIRM,
    LINE_STATUS_PENDING_SELECT,
    MATCH_COLOR_GREEN,
    MATCH_COLOR_RED,
    MATCH_COLOR_YELLOW,
    MATCH_TYPE_EXACT,
    MATCH_TYPE_MULTIPLE,
    MATCH_TYPE_NONE,
    build_match_review,
    dedupe_aliases,
    embedding_acceptable_for_match,
    evaluate_product_code_embedding_gate,
    extract_line_product_code,
    normalize_product_name,
    names_are_close_spelling_variant,
    product_code_in_candidate_codes,
    resolve_match_state_from_count,
    score_alias_match,
)


class TestProductMatchingCommon(unittest.TestCase):
    def test_normalize_product_name(self):
        self.assertEqual(normalize_product_name("  Paracetamol-500mg!  "), "paracetamol 500mg")

    def test_names_are_close_spelling_variant(self):
        self.assertTrue(names_are_close_spelling_variant("Brownys", "Brownies"))
        self.assertFalse(names_are_close_spelling_variant("Cookies", "Brownies"))

    def test_score_exact_alias(self):
        self.assertEqual(score_alias_match("Widget", "widget"), (0, "exact"))

    def test_score_contains_alias_invoice_mode(self):
        self.assertEqual(
            score_alias_match("para", "paracetamol", allow_contains=True),
            (1, "contains"),
        )
        self.assertIsNone(score_alias_match("para", "paracetamol"))

    def test_dedupe_aliases(self):
        self.assertEqual(
            dedupe_aliases(["Widget", " widget ", "Other"]),
            ["Widget", "Other"],
        )

    def test_build_match_review(self):
        review = build_match_review(
            product_name="Test",
            match_type="multiple",
            line_status="PENDING_SELECT",
            match_color="yellow",
            ranked_candidates=[{"productId": 1}],
        )
        self.assertEqual(review["matchColor"], "yellow")
        self.assertEqual(len(review["rankedCandidates"]), 1)

    def test_resolve_match_state_from_count(self):
        green = resolve_match_state_from_count(1, single_match_type=MATCH_TYPE_EXACT)
        self.assertEqual(green.match_color, MATCH_COLOR_GREEN)
        self.assertEqual(green.line_status, LINE_STATUS_PENDING_CONFIRM)
        self.assertEqual(green.match_type, MATCH_TYPE_EXACT)

        yellow = resolve_match_state_from_count(3)
        self.assertEqual(yellow.match_color, MATCH_COLOR_YELLOW)
        self.assertEqual(yellow.line_status, LINE_STATUS_PENDING_SELECT)
        self.assertEqual(yellow.match_type, MATCH_TYPE_MULTIPLE)

        red = resolve_match_state_from_count(0)
        self.assertEqual(red.match_color, MATCH_COLOR_RED)
        self.assertEqual(red.line_status, LINE_STATUS_NO_MATCH)
        self.assertEqual(red.match_type, MATCH_TYPE_NONE)

    def test_embedding_acceptable_for_match(self):
        self.assertTrue(embedding_acceptable_for_match("A", "B", 0.05))
        self.assertTrue(embedding_acceptable_for_match("Widget", "Widget", 0.10))
        self.assertFalse(embedding_acceptable_for_match("A", "B", 0.20))

    def test_describe_embedding_acceptance(self):
        from services.product_matching_common import describe_embedding_acceptance

        ok, reason = describe_embedding_acceptance("Widget", "Widget", 0.07)
        self.assertTrue(ok)
        self.assertIn("strong", reason)

        ok, reason = describe_embedding_acceptance("Widget", "Widget", 0.11)
        self.assertTrue(ok)
        self.assertIn("exact_name", reason)

        ok, reason = describe_embedding_acceptance("A", "B", 0.11)
        self.assertFalse(ok)
        self.assertIn("name_mismatch", reason)

    def test_extract_line_product_code(self):
        self.assertEqual(
            extract_line_product_code({"itemNo": " SUP00043 ", "description": "X"}),
            "SUP00043",
        )
        self.assertIsNone(extract_line_product_code({"description": "Only name"}))

    def test_product_code_in_candidate_codes(self):
        self.assertTrue(product_code_in_candidate_codes("sup00043", ["SUP00042", "SUP00043"]))
        self.assertFalse(product_code_in_candidate_codes("ERP00044", ["SUP00043"]))

    @mock.patch(
        "services.product_matching_common.embedding_distance_between",
        return_value=0.05,
    )
    def test_evaluate_product_code_embedding_gate_pass(self, _mock_dist):
        present, ok, distance, reason = evaluate_product_code_embedding_gate(
            product_name="Brownies",
            product_code="SUP00044",
            candidate_name="Brownies",
            candidate_codes=["SUP00044"],
        )
        self.assertTrue(present)
        self.assertTrue(ok)
        self.assertEqual(distance, 0.05)
        self.assertIsNone(reason)

    @mock.patch(
        "services.product_matching_common.embedding_distance_between",
        return_value=0.25,
    )
    def test_evaluate_product_code_embedding_gate_fail(self, _mock_dist):
        present, ok, distance, reason = evaluate_product_code_embedding_gate(
            product_name="Brownys",
            product_code="SUP00043",
            candidate_name="MARIE GOLD BISCUITS",
            candidate_codes=["SUP00043"],
        )
        self.assertTrue(present)
        self.assertFalse(ok)
        self.assertEqual(reason, "product_code_embedding_mismatch")


if __name__ == "__main__":
    unittest.main()
