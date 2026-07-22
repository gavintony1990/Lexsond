from __future__ import annotations

import unittest

from lexsond.evaluations.scorers import (
    EvaluationScoringError,
    ScoreStatus,
    get_scorer,
    list_scorers,
)


class EvaluationScorerTests(unittest.TestCase):
    def test_registry_exposes_only_bounded_deterministic_scorers(self) -> None:
        self.assertEqual(
            [item.scorer_id for item in list_scorers()],
            [
                "contains_all",
                "exact_match",
                "json_schema_valid",
                "multiple_choice_accuracy",
                "normalized_exact_match",
                "regex_match",
                "token_f1",
            ],
        )
        for descriptor in list_scorers():
            self.assertRegex(descriptor.version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
            self.assertNotIn("judge", descriptor.scorer_id.lower())

    def test_exact_and_normalized_matching_have_versioned_semantics(self) -> None:
        exact = get_scorer("exact_match")
        normalized = get_scorer("normalized_exact_match")

        self.assertEqual(
            exact.score("Alpha", {"answer": "Alpha"}).status,
            ScoreStatus.PASS,
        )
        self.assertEqual(
            exact.score("alpha", {"answer": "Alpha"}).status,
            ScoreStatus.FAIL,
        )
        folded = normalized.score("  Ａlpha，  BETA! ", {"answer": "alpha beta"})
        self.assertEqual(folded.status, ScoreStatus.PASS)
        self.assertEqual(folded.score, 1.0)
        self.assertEqual(folded.facts["normalization"], "nfkc-casefold-ws-punct/v1")

    def test_missing_or_unparseable_evidence_is_unknown_not_an_invented_zero(self) -> None:
        cases = (
            ("exact_match", "answer", {}),
            ("multiple_choice_accuracy", "not a choice", {"answer_index": 2, "choice_count": 4}),
            ("json_schema_valid", "not json", {"schema": {"type": "object"}}),
        )
        for scorer_id, output, reference in cases:
            with self.subTest(scorer_id=scorer_id):
                result = get_scorer(scorer_id).score(output, reference)
                self.assertEqual(result.status, ScoreStatus.UNKNOWN)
                self.assertIsNone(result.score)

    def test_token_f1_contains_all_and_multiple_choice_are_deterministic(self) -> None:
        cases = (
            ("token_f1", "red blue blue", {"answer": "red blue"}),
            ("contains_all", "signal green; status ready", {"values": ["green", "ready"]}),
            ("multiple_choice_accuracy", "C", {"answer_index": 2, "choice_count": 4}),
        )
        for scorer_id, output, reference in cases:
            scorer = get_scorer(scorer_id)
            first = scorer.score(output, reference)
            for _ in range(100):
                self.assertEqual(scorer.score(output, reference), first)

    def test_regex_scorer_rejects_redos_constructs_and_never_echoes_output(self) -> None:
        scorer = get_scorer("regex_match")
        for pattern in ("(a+)+$", "(a|aa)+$", "a{1,100000}", r"(a)\1", "(?=a)a", "a*a*a*a*b"):
            with self.subTest(pattern=pattern), self.assertRaises(EvaluationScoringError):
                scorer.validate_reference({"pattern": pattern})
        result = scorer.score("ticket-2048", {"pattern": r"^ticket-[0-9]+$"})
        self.assertEqual(result.status, ScoreStatus.PASS)
        self.assertNotIn("ticket-2048", repr(result.facts))

    def test_json_schema_subset_limits_depth_nodes_and_keywords(self) -> None:
        scorer = get_scorer("json_schema_valid")
        valid_reference = {
            "schema": {
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string", "enum": ["ready"]}},
                "additionalProperties": False,
            }
        }
        self.assertEqual(
            scorer.score('{"status":"ready"}', valid_reference).status,
            ScoreStatus.PASS,
        )
        self.assertEqual(
            scorer.score('{"status":"offline"}', valid_reference).status,
            ScoreStatus.FAIL,
        )
        with self.assertRaises(EvaluationScoringError):
            scorer.validate_reference({"schema": {"$ref": "https://example.invalid/schema"}})
        deep: dict[str, object] = {"type": "string"}
        for _ in range(10):
            deep = {"type": "object", "properties": {"next": deep}}
        with self.assertRaises(EvaluationScoringError):
            scorer.validate_reference({"schema": deep})

        array_reference = {
            "schema": {"type": "array", "items": {"type": "number"}}
        }
        oversized = "[" + ",".join("1" for _ in range(300)) + "]"
        limited = scorer.score(oversized, array_reference)
        self.assertEqual(limited.status, ScoreStatus.UNKNOWN)
        self.assertIsNone(limited.score)
        nonfinite = scorer.score("[NaN]", array_reference)
        self.assertEqual(nonfinite.status, ScoreStatus.UNKNOWN)
        self.assertIsNone(nonfinite.score)


if __name__ == "__main__":
    unittest.main()
