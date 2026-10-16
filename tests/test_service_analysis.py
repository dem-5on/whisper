from __future__ import annotations

import unittest

from whisper_service.analysis import (
    AnalysisError,
    OpenRouterAnalysisModel,
    build_prompt,
    parse_result,
    split_transcript,
)


class AnalysisUnitTests(unittest.TestCase):
    def test_prompts_cover_every_planned_kind(self) -> None:
        for kind in ("notes", "topics", "terms", "questions", "translation", "summary", "actions", "decisions"):
            system, user = build_prompt(kind, "hello world", language="twi")
            self.assertTrue(system.strip())
            self.assertEqual(user, "hello world")
        with self.assertRaises(AnalysisError):
            build_prompt("teleport", "hello")

    def test_long_transcripts_split_wordsafely(self) -> None:
        text = " ".join(f"word{i}" for i in range(5000))
        chunks = split_transcript(text, max_chars=1000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1100)
        self.assertEqual(" ".join(chunks).split(), text.split())
        self.assertEqual(split_transcript("  "), [])

    def test_text_stages_need_real_content(self) -> None:
        self.assertEqual(parse_result("notes", "# Notes\n- point").startswith("# Notes"), True)
        with self.assertRaises(AnalysisError):
            parse_result("translation", "   ")

    def test_topics_clamp_bad_indices(self) -> None:
        parsed = parse_result(
            "topics",
            '[{"title": "Intro", "summary": "Start", "start_segment": 0, "end_segment": 4},'
            ' {"title": "", "start_segment": 1},'
            ' {"title": "Late", "start_segment": 99, "end_segment": 100}]',
            segment_count=3,
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual((parsed[0]["start_segment"], parsed[0]["end_segment"]), (0, 0))
        self.assertEqual(parsed[1], {"title": "Late", "summary": "", "start_segment": 0, "end_segment": 0})

    def test_topics_reject_empty_and_garbage(self) -> None:
        with self.assertRaises(AnalysisError):
            parse_result("topics", "[]")
        with self.assertRaises(AnalysisError):
            parse_result("topics", "not json{")

    def test_terms_questions_actions_shapes(self) -> None:
        terms = parse_result("terms", '[{"term": "Photosynthesis", "definition": "Plants make food."}, {"term": "x"}]')
        self.assertEqual(terms, [{"term": "Photosynthesis", "definition": "Plants make food."}])
        questions = parse_result("questions", '["Why?", {"question": "How?"}]')
        self.assertEqual(questions, ["Why?", "How?"])
        actions = parse_result("actions", '["Ship it", {"action": "Call Ama", "owner": "Kwame"}]')
        self.assertEqual(actions, [{"action": "Ship it", "owner": ""}, {"action": "Call Ama", "owner": "Kwame"}])
        decisions = parse_result("decisions", '["Ship Friday", {"decision": "Delay launch"}]')
        self.assertEqual(decisions, ["Ship Friday", "Delay launch"])
        with self.assertRaises(AnalysisError):
            parse_result("decisions", "[]")
        with self.assertRaises(AnalysisError):
            parse_result("actions", "[]")
        with self.assertRaises(AnalysisError):
            parse_result("teleport", "{}")

    def test_model_construction_validates(self) -> None:
        with self.assertRaises(ValueError):
            OpenRouterAnalysisModel("  ", ("m/one",))
        with self.assertRaises(ValueError):
            OpenRouterAnalysisModel("key", ())
        model = OpenRouterAnalysisModel("key", ("m/one", "m/two"))
        self.assertEqual(model.models, ("m/one", "m/two"))
