from __future__ import annotations

import unittest

from evals.audio_eval import main as audio_main
from evals.audio_eval import word_error_rate
from evals.lecture_eval import FIXTURES, evaluate_fixture_outputs, main


class LectureEvalTests(unittest.TestCase):
    def test_fixtures_cover_multilingual_and_edge_cases(self) -> None:
        ids = {fixture.id for fixture in FIXTURES}
        self.assertTrue({"english-biology", "twi-english-codeswitch", "french-english-mixed",
                         "jargon-software", "short-fragment", "multi-topic-long"} <= ids)
        for fixture in FIXTURES:
            self.assertTrue(fixture.transcript.strip())

    def test_contract_mode_passes_good_and_rejects_bad(self) -> None:
        self.assertEqual(main(), 0)

    def test_word_error_rate_basics(self) -> None:
        self.assertEqual(word_error_rate("hello world", "hello world"), 0.0)
        self.assertGreater(word_error_rate("hello world", "hello"), 0.0)
        self.assertEqual(word_error_rate("", ""), 0.0)

    def test_audio_runner_explains_itself_without_clips(self) -> None:
        self.assertEqual(audio_main(), 2)
