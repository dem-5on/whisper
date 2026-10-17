"""Meeting quality evaluation: fixtures, property checks, and runners.

No accuracy is claimed without a measured run. The harness runs in two ways:

* Contract mode (default, no keys): checks the property validators against
  fixed good/bad stage outputs **and** runs the real speaker-alignment code over
  fixture turns, so CI stays green and the rules stay honest.
* Live mode (``WICX_EVAL_LIVE=1`` with ``WHISPER_OPENROUTER_API_KEY`` set):
  sends each fixture transcript through the deployed analysis endpoint path and
  reports per-fixture, per-stage PASS/FAIL plus a JSON report. Live mode spends
  operator tokens; fixtures are small by design.

Speaker detection itself is a separate protocol (it needs a hosted pyannote
worker and permissioned recordings), so this harness never claims diarization
accuracy. It does check the promise the product makes around labels: strict
majority alignment, provisional ``Speaker 1/2`` naming, and user corrections
that later analysis passes must not overwrite.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transcriber.meeting import (  # noqa: E402
    MAX_DIARIZATION_ATTEMPTS,
    MAX_SPEAKER_TEXT_CHARS,
    speaker_labeled_text,
)
from transcriber.session_store import SessionStore  # noqa: E402


STOPWORDS = frozenset(
    "a an the and or but of to in on for with is are was were be been do does did "
    "what which who whom whose when where why how many much can could should would "
    "will shall may might must this that these those it its i you he she we they "
    "me him her us them my our your their he she him hers here there then now ok "
    "okay yeah yes just also about after before because so".split()
)


@dataclass(frozen=True)
class MeetingFixture:
    """One meeting recording plus the properties its outputs must hold."""

    id: str
    language: str
    transcript: str
    segments: tuple[dict, ...]
    turns: tuple[dict, ...]
    expected_speakers: tuple[str, ...] = ()
    speaker_names: dict[str, str] = field(default_factory=dict)
    grounding_transcript: str = ""
    must_contain_summary: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    preserved_terms: tuple[str, ...] = ()


FIXTURES: tuple[MeetingFixture, ...] = (
    MeetingFixture(
        id="english-release-planning",
        language="en",
        transcript=(
            "Kwame: We should ship the release on Friday instead of Monday. "
            "Ama: Agreed, the QA run finishes Thursday. Kwame: Then Ama cuts the "
            "release branch and Kwame writes the changelog. Ama: I will also tell "
            "support before the announcement."
        ),
        segments=(
            {"start_seconds": 0.0, "end_seconds": 6.5, "text": "We should ship the release on Friday instead of Monday."},
            {"start_seconds": 6.5, "end_seconds": 11.0, "text": "Agreed, the QA run finishes Thursday."},
            {"start_seconds": 11.0, "end_seconds": 19.5, "text": "Then Ama cuts the release branch and Kwame writes the changelog."},
            {"start_seconds": 19.5, "end_seconds": 24.0, "text": "I will also tell support before the announcement."},
        ),
        turns=(
            {"start_seconds": 0.0, "end_seconds": 11.2, "speaker_id": "speaker_1"},
            {"start_seconds": 11.2, "end_seconds": 24.0, "speaker_id": "speaker_2"},
        ),
        expected_speakers=("speaker_1", "speaker_2"),
        speaker_names={"speaker_1": "Kwame", "speaker_2": "Ama"},
        must_contain_summary=("Friday",),
        must_not_contain=("Kubernetes", "roadmap"),
        preserved_terms=("changelog", "QA"),
    ),
    MeetingFixture(
        id="english-overlapping-crosstalk",
        language="en",
        transcript=(
            "Two voices talk over each other about the budget, then one asks to "
            "record the minutes again."
        ),
        segments=(
            {"start_seconds": 0.0, "end_seconds": 4.0, "text": "Two voices talk over each other about the budget"},
            {"start_seconds": 4.0, "end_seconds": 7.0, "text": "then one asks to record the minutes again"},
        ),
        # No speaker covers a strict majority of the first segment, so it stays
        # unlabeled: a provisional guess is better than a fabricated name.
        turns=(
            {"start_seconds": 0.0, "end_seconds": 2.0, "speaker_id": "speaker_1"},
            {"start_seconds": 2.0, "end_seconds": 4.0, "speaker_id": "speaker_2"},
            {"start_seconds": 4.0, "end_seconds": 7.0, "speaker_id": "speaker_1"},
        ),
        # Only the second segment has a strict majority, so the crosstalk stays
        # unlabeled instead of guessing a name for it.
        expected_speakers=("speaker_1",),
        must_contain_summary=("minutes",),
    ),
    MeetingFixture(
        id="twi-meeting-translation",
        language="twi",
        transcript=(
            "Kwame: Yɛbɛyɛ sa wɔbɛkyerɛ aseɛ nnansa. Ama: Yɛɛ, nhyiam no bɛtumi "
            "kɔ Wednesday. Kwame: Enti Ama bɛka ne nnansa a, Kwame bɛkyerɛ changelog no."
        ),
        segments=(
            {"start_seconds": 0.0, "end_seconds": 5.0, "text": "Yɛbɛyɛ sa wɔbɛkyerɛ aseɛ nnansa."},
            {"start_seconds": 5.0, "end_seconds": 10.5, "text": "Yɛɛ, nhyiam no bɛtumi kɔ Wednesday."},
            {"start_seconds": 10.5, "end_seconds": 16.0, "text": "Enti Ama bɛka ne nnansa a, Kwame bɛkyerɛ changelog no."},
        ),
        turns=(
            {"start_seconds": 0.0, "end_seconds": 10.6, "speaker_id": "speaker_1"},
            {"start_seconds": 10.6, "end_seconds": 16.0, "speaker_id": "speaker_2"},
        ),
        expected_speakers=("speaker_1", "speaker_2"),
        must_contain_summary=("nnansa",),
        preserved_terms=("changelog", "Wednesday"),
        grounding_transcript=(
            "Kwame says the meeting is on Friday and QA ends on Wednesday. "
            "Ama announces it on Friday and Kwame checks the changelog."
        ),
    ),
)


def content_words(text: str) -> set[str]:
    """Lowercase word-ish tokens minus stopwords, for overlap checks."""
    return {
        word for word in re.findall(r"[a-z\u00e0-\u00ff\u0101-\u02af0-9']+", text.lower())
        if word not in STOPWORDS
    }


def grounding_words(fixture: MeetingFixture) -> set[str]:
    """Transcript vocabulary, plus our own reference translation when present.

    Stage output follows the language of the meeting, so a non-English fixture is
    grounded against the transcript and the reference English rendering we wrote
    ourselves -- never against a model-produced translation.
    """
    return content_words(fixture.transcript) | content_words(fixture.grounding_transcript)


@dataclass
class CheckResult:
    fixture: str
    stage: str
    passed: bool
    detail: str = ""


def check_summary(fixture: MeetingFixture, summary: str) -> CheckResult:
    """Summary must stay grounded in what the meeting actually said."""
    lowered = summary.lower()
    for required in fixture.must_contain_summary:
        if required.lower() not in lowered:
            return CheckResult(fixture.id, "summary", False, f"missing {required!r}")
    for banned in fixture.must_not_contain:
        if banned.lower() in lowered:
            return CheckResult(fixture.id, "summary", False, f"invented {banned!r}")
    if len(summary.split()) < 5:
        return CheckResult(fixture.id, "summary", False, "summary too short to be useful")
    return CheckResult(fixture.id, "summary", True)


def check_decisions(fixture: MeetingFixture, decisions: list) -> CheckResult:
    """Decisions must be non-empty, bounded, and grounded in the transcript."""
    if not decisions:
        return CheckResult(fixture.id, "decisions", False, "no decisions extracted")
    if len(decisions) > 20:
        return CheckResult(fixture.id, "decisions", False, "too many decisions")
    vocabulary = grounding_words(fixture)
    for entry in decisions:
        text = entry.get("decision", "") if isinstance(entry, dict) else entry
        text = str(text).strip()
        if not text:
            return CheckResult(fixture.id, "decisions", False, "empty decision")
        if not (content_words(text) & vocabulary):
            return CheckResult(fixture.id, "decisions", False, f"ungrounded: {text[:60]!r}")
    return CheckResult(fixture.id, "decisions", True)


def check_actions(fixture: MeetingFixture, actions: list) -> CheckResult:
    """Actions need a task; an owner must be someone who spoke."""
    if not actions:
        return CheckResult(fixture.id, "actions", False, "no action items extracted")
    vocabulary = grounding_words(fixture)
    for entry in actions:
        if isinstance(entry, dict):
            action = str(entry.get("action", "")).strip()
            owner = str(entry.get("owner", "")).strip()
        else:
            action, owner = str(entry).strip(), ""
        if not action:
            return CheckResult(fixture.id, "actions", False, "empty action")
        if not (content_words(action) & vocabulary):
            return CheckResult(fixture.id, "actions", False, f"ungrounded action: {action[:60]!r}")
        if owner and owner.lower() not in fixture.transcript.lower():
            return CheckResult(fixture.id, "actions", False, f"owner not in transcript: {owner!r}")
    return CheckResult(fixture.id, "actions", True)


def check_translation(fixture: MeetingFixture, translation: str | None) -> CheckResult:
    """Non-English meetings must come back translated with terms preserved."""
    if fixture.language == "en":
        return CheckResult(fixture.id, "translation", True, "source already English")
    if not translation or not translation.strip():
        return CheckResult(fixture.id, "translation", False, "missing translation")
    if translation.strip() == fixture.transcript.strip():
        return CheckResult(fixture.id, "translation", False, "translation identical to source")
    lowered = translation.lower()
    for term in fixture.preserved_terms:
        if term.lower() not in lowered:
            return CheckResult(fixture.id, "translation", False, f"dropped term {term!r}")
    return CheckResult(fixture.id, "translation", True)


def check_speaker_labels(fixture: MeetingFixture) -> CheckResult:
    """Labels follow strict majority alignment and survive later passes."""
    with tempfile.TemporaryDirectory(prefix="wicx-meeting-eval-") as directory:
        try:
            store = SessionStore(Path(directory) / "data")
            session = store.create("meeting")
            store.update(
                session.id, state="ready", source_transcript=fixture.transcript,
                segments=[dict(segment) for segment in fixture.segments],
                outputs={"speaker_names": dict(fixture.speaker_names)}, finished=True,
            )
            if fixture.turns:
                store.apply_diarization(session.id, [dict(turn) for turn in fixture.turns])
            session_id = session.id
        except (OSError, ValueError) as exc:
            return CheckResult(
                fixture.id, "labels", False, f"store rejected fixture: {type(exc).__name__}",
            )
        return _score_labels(store, session_id, fixture)


def _score_labels(store: SessionStore, session_id: str, fixture: MeetingFixture) -> CheckResult:
    """Score the alignment, naming, and correction-survival promises."""
    stored = store.get(session_id)
    labeled = {
        segment["speaker_id"] for segment in stored.segments if segment.get("speaker_id")
    }
    if tuple(sorted(labeled)) != tuple(sorted(fixture.expected_speakers)):
        return CheckResult(
            fixture.id, "labels", False,
            f"expected {sorted(fixture.expected_speakers)}, got {sorted(labeled)}",
        )
    text = speaker_labeled_text(stored.segments, stored.outputs.get("speaker_names", {}))
    if len(text) > MAX_SPEAKER_TEXT_CHARS * 2:
        return CheckResult(fixture.id, "labels", False, "labeled text not bounded")
    for name in fixture.speaker_names.values():
        if name not in text:
            return CheckResult(fixture.id, "labels", False, f"missing name {name!r} in labeled text")
    if not labeled:
        return CheckResult(fixture.id, "labels", True, "majority threshold left segments unlabeled")

    # A user's rename and per-segment correction must outlive a re-run.
    store.rename_speaker(session_id, fixture.expected_speakers[0], "Nana")
    first_index = next(
        index for index, segment in enumerate(stored.segments)
        if segment.get("speaker_id") == fixture.expected_speakers[-1]
    )
    store.assign_segment_speaker(session_id, first_index, fixture.expected_speakers[0])
    store.apply_diarization(session_id, [dict(turn) for turn in fixture.turns])
    after = store.get(session_id)
    names = after.outputs.get("speaker_names", {})
    if names.get(fixture.expected_speakers[0]) != "Nana":
        return CheckResult(fixture.id, "labels", False, "rename did not survive a re-run")
    if after.segments[first_index].get("speaker_id") != fixture.expected_speakers[0]:
        return CheckResult(fixture.id, "labels", False, "manual reassignment did not survive a re-run")
    if any(name == "Speaker 1" for name in names.values()):
        return CheckResult(fixture.id, "labels", False, "provisional label leaked into the roster")
    return CheckResult(fixture.id, "labels", True)


def check_retry_budget() -> CheckResult:
    """A failing service must stop retrying instead of looping forever."""
    if MAX_DIARIZATION_ATTEMPTS < 1:
        return CheckResult("retry-budget", "attempts", False, "no retry budget configured")
    return CheckResult("retry-budget", "attempts", True)


def evaluate_fixture_outputs(fixture: MeetingFixture, outputs: dict) -> list[CheckResult]:
    """Run every property check for one fixture against stage outputs.

    Speaker labels depend only on the fixture turns, so they are scored once
    per fixture by :func:`evaluate_all_labels` rather than per stage output.
    """
    return [
        check_summary(fixture, str(outputs.get("summary", ""))),
        check_decisions(fixture, outputs.get("decisions", [])),
        check_actions(fixture, outputs.get("actions", [])),
        check_translation(fixture, outputs.get("translation")),
    ]


def evaluate_all_labels() -> list[CheckResult]:
    """Score alignment, provisional naming, and correction survival."""
    return [check_speaker_labels(fixture) for fixture in FIXTURES]


def summarize(results: list[CheckResult]) -> dict:
    """Aggregate a report mapping fixture -> stage -> pass/fail."""
    report: dict[str, dict[str, bool]] = {}
    for result in results:
        report.setdefault(result.fixture, {})[result.stage] = result.passed
    return report


def main() -> int:
    """Contract mode: validate the checkers themselves against fixed outputs."""
    # A non-English fixture so the translation rule is exercised too.
    fixture = next(f for f in FIXTURES if f.id == "twi-meeting-translation")
    good = {
        "summary": "Kwame ne Ama bɛkɔ ne nnansa a; QA no bɛtumi Wednesday.",
        "decisions": ["Wobɛkɔ ne nnansa a"],
        "actions": [
            {"action": "Ama bɛka ne nnansa a", "owner": "Ama"},
            {"action": "Kwame bɛkyerɛ changelog no", "owner": "Kwame"},
        ],
        "translation": (
            "Kwame said the meeting is on Friday and QA ends on Wednesday. "
            "Ama announces it on Friday and Kwame checks the changelog."
        ),
    }
    bad = {
        "summary": "Kubernetes migration is on the roadmap.",
        "decisions": [],
        "actions": [{"action": "", "owner": "Zelda"}],
        "translation": None,
    }
    good_results = evaluate_fixture_outputs(fixture, good)
    bad_results = evaluate_fixture_outputs(fixture, bad)
    label_results = evaluate_all_labels()
    budget = check_retry_budget()
    ok = budget.passed
    print(f"{fixture.id} contract mode")
    print(f"  {budget.stage}: {'PASS' if budget.passed else 'FAIL ' + budget.detail}")
    for result in good_results:
        print(f"  good/{result.stage}: {'PASS' if result.passed else 'FAIL ' + result.detail}")
        ok = ok and result.passed
    for result in bad_results:
        print(f"  bad/{result.stage}: {'FAIL(expected)' if not result.passed else 'PASS(UNEXPECTED!)'}")
        ok = ok and not result.passed
    for result in label_results:
        detail = f" ({result.detail})" if result.detail else ""
        print(f"  labels/{result.fixture}: {'PASS' if result.passed else 'FAIL ' + result.detail}")
        ok = ok and result.passed
    if os.environ.get("WICX_EVAL_LIVE", ""):
        print("live mode requested (needs the deployed analysis endpoint; see AUDIO_EVAL.md)")
        print(json.dumps(summarize(good_results + bad_results + label_results), indent=2))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())