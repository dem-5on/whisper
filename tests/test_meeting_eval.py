from __future__ import annotations

import unittest

from evals.meeting_eval import (
    FIXTURES,
    check_actions,
    check_decisions,
    check_retry_budget,
    check_speaker_labels,
    check_summary,
    check_translation,
    evaluate_all_labels,
    main,
)


class MeetingEvalTests(unittest.TestCase):
    def test_fixtures_cover_multilingual_and_edge_cases(self) -> None:
        ids = {fixture.id for fixture in FIXTURES}
        self.assertTrue({
            "english-release-planning", "english-overlapping-crosstalk", "twi-meeting-translation",
        } <= ids)
        for fixture in FIXTURES:
            self.assertTrue(fixture.transcript.strip())
            self.assertTrue(fixture.segments)
            self.assertTrue(fixture.turns)
            bounds = [segment["end_seconds"] for segment in fixture.segments]
            for index, segment in enumerate(fixture.segments):
                self.assertLess(segment["start_seconds"], segment["end_seconds"])
                if index:
                    self.assertLessEqual(segment["start_seconds"], fixture.segments[index - 1]["end_seconds"])
            for turn in fixture.turns:
                self.assertLess(turn["start_seconds"], turn["end_seconds"])
                self.assertLessEqual(turn["end_seconds"], max(bounds) + 0.5)

    def test_contract_mode_passes_good_and_rejects_bad(self) -> None:
        self.assertEqual(main(), 0)

    def test_labels_score_alignment_and_correction_survival(self) -> None:
        results = {result.fixture: result for result in evaluate_all_labels()}
        self.assertTrue(all(result.passed for result in results.values()), results)
        fixture = next(f for f in FIXTURES if f.id == "english-release-planning")
        self.assertTrue(check_speaker_labels(fixture).passed)
        self.assertTrue(check_retry_budget().passed)

    def test_crosstalk_segment_without_a_majority_stays_unlabeled(self) -> None:
        import tempfile
        from pathlib import Path

        from transcriber.session_store import SessionStore

        fixture = next(f for f in FIXTURES if f.id == "english-overlapping-crosstalk")
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory) / "data")
            session = store.create("meeting")
            store.update(
                session.id, state="ready", source_transcript=fixture.transcript,
                segments=[dict(segment) for segment in fixture.segments], finished=True,
            )
            store.apply_diarization(session.id, [dict(turn) for turn in fixture.turns])
            segments = store.get(session.id).segments
        self.assertIsNone(segments[0]["speaker_id"])
        self.assertEqual(segments[1]["speaker_id"], "speaker_1")

    def test_summary_decisions_actions_and_translation_reject_inventions(self) -> None:
        fixture = next(f for f in FIXTURES if f.id == "english-release-planning")
        self.assertTrue(check_summary(fixture, "Ship Friday after QA closes Thursday.").passed)
        self.assertFalse(check_summary(fixture, "Kubernetes migration").passed)
        self.assertTrue(check_decisions(fixture, ["Ship the release on Friday"]).passed)
        self.assertFalse(check_decisions(fixture, ["Buy a new office building"]).passed)
        self.assertTrue(check_actions(fixture, [{"action": "Cut the changelog", "owner": ""}]).passed)
        self.assertFalse(check_actions(fixture, [{"action": "Cut the changelog", "owner": "Zelda"}]).passed)
        self.assertFalse(check_actions(fixture, [{"action": "", "owner": "Kwame"}]).passed)
        self.assertTrue(check_translation(fixture, None).passed)  # English source needs none

        twi = next(f for f in FIXTURES if f.id == "twi-meeting-translation")
        self.assertFalse(check_translation(twi, None).passed)
        self.assertFalse(check_translation(twi, twi.transcript).passed)
        self.assertTrue(
            check_translation(
                twi, "The changelog is checked before Wednesday and the news goes out Friday."
            ).passed,
        )
        self.assertFalse(check_translation(twi, "Everything is fine.").passed)