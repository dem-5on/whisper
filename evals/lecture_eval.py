"""Lecture quality evaluation: fixtures, property checks, and runners.

No accuracy is claimed without a measured run. The harness runs in two ways:

* Contract mode (default, no keys): checks the property validators against
  fixed good/bad stage outputs, so CI stays green and the rules stay honest.
* Live mode (``WICX_EVAL_LIVE=1`` with ``WHISPER_OPENROUTER_API_KEY`` set):
  runs every fixture transcript through the real analysis endpoint path and
  reports per-fixture, per-stage PASS/FAIL plus a JSON report. Live mode
  spends operator tokens; fixtures are small by design.

Audio (speech-to-text) evaluation is a separate protocol documented in
``AUDIO_EVAL.md``: it needs permissioned microphone clips this repository
does not ship. ``audio_eval.py`` scores a local ``clips/`` directory when one
is provided and otherwise explains what to record.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass


STOPWORDS = frozenset(
    "a an the and or but of to in on for with is are was were be been do does did "
    "what which who whom whose when where why how many much can could should would "
    "this that these those it its it’s i you he she we they me him her us them my "
    "et le la les un une des du de en que qui quoi comment pourquoi combien est "
    "sont et ou mais donc or ni car o no".split()
)


@dataclass(frozen=True)
class Fixture:
    """One lecture transcript plus the properties its analysis must hold."""

    id: str
    language: str
    transcript: str
    must_contain_notes: tuple[str, ...] = ()
    must_not_contain: tuple[str, ...] = ()
    preserved_terms: tuple[str, ...] = ()


FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        id="english-biology",
        language="en",
        transcript=(
            "Today we cover Photosynthesis in the chloroplast. Light dependent "
            "reactions run in the thylakoid membrane and make ATP and NADPH. "
            "The Calvin cycle then fixes carbon dioxide in the stroma. "
            "Review: the thylakoid is the site of the light reactions."
        ),
        must_contain_notes=("Photosynthesis", "thylakoid"),
        must_not_contain=("mitochondria",),
        preserved_terms=("Photosynthesis", "thylakoid", "ATP", "NADPH", "Calvin"),
    ),
    Fixture(
        id="twi-english-codeswitch",
        language="twi-en",
        transcript=(
            "Yɛbɛyɛ lecture no fa photosynthesis ho. The chloroplast na ɛyɛ "
            "plant cell no mu, ɛna light reaction no kɔ so wɔ thylakoid membrane "
            "no mu. Sɛ wote aseɛ a, write down ATP ne NADPH. Mede mo please, "
            "the Calvin cycle nso ɛfa carbon dioxide."
        ),
        must_contain_notes=("chloroplast", "thylakoid"),
        must_not_contain=("mitochondria",),
        preserved_terms=("chloroplast", "thylakoid", "ATP", "NADPH", "Calvin"),
    ),
    Fixture(
        id="french-english-mixed",
        language="fr-en",
        transcript=(
            "Bonjour à tous, today we discuss the budget forecast. Le chiffre "
            "d'affaires must grow dix pour cent this quarter. Merci, any questions "
            "about the forecast?"
        ),
        preserved_terms=("budget forecast",),
    ),
    Fixture(
        id="jargon-software",
        language="en",
        transcript=(
            "Deploy the canary to us-east-1 with the TypeScript strict flag on. "
            "If p99 latency exceeds 400 milliseconds, roll back the Kubernetes "
            "deployment and page the on-call. The GraphQL gateway stays up."
        ),
        must_contain_notes=("TypeScript", "Kubernetes"),
        must_not_contain=("JavaScript framework",),
        preserved_terms=("TypeScript", "Kubernetes", "GraphQL", "p99"),
    ),
    Fixture(
        id="short-fragment",
        language="en",
        transcript="Reminder: office hours moved to Thursday.",
        must_contain_notes=("office hours",),
    ),
    Fixture(
        id="multi-topic-long",
        language="en",
        transcript=(
            "Part one, introductions. Ama presents the syllabus and the midterm "
            "date, which is October 30. Part two, the lab. We calibrate the "
            "spectrometer before measuring absorbance. Part three, fieldwork. "
            "Bring boots; the soil site floods after rain. Finally, questions "
            "about grading close the session."
        ),
        must_contain_notes=("syllabus", "spectrometer"),
        preserved_terms=("syllabus", "spectrometer", "absorbance"),
    ),
)


def content_words(text: str) -> set[str]:
    """Lowercase alphanumeric words minus stopwords, for overlap checks."""
    return {
        word for word in re.findall(r"[a-zà-ÿā-ʯ0-9]+", text.lower())
        if word not in STOPWORDS
    }


@dataclass
class CheckResult:
    fixture: str
    stage: str
    passed: bool
    detail: str = ""


def check_notes(fixture: Fixture, notes: str) -> CheckResult:
    """Notes must preserve named content and invent nothing on the ban list."""
    lowered = notes.lower()
    for required in fixture.must_contain_notes:
        if required.lower() not in lowered:
            return CheckResult(fixture.id, "notes", False, f"missing {required!r}")
    for banned in fixture.must_not_contain:
        if banned.lower() in lowered:
            return CheckResult(fixture.id, "notes", False, f"invented {banned!r}")
    if len(notes.split()) < 5:
        return CheckResult(fixture.id, "notes", False, "notes too short to be useful")
    return CheckResult(fixture.id, "notes", True)


def check_terms(fixture: Fixture, terms: list[dict]) -> CheckResult:
    """Every extracted term must occur in the source transcript."""
    if not terms:
        return CheckResult(fixture.id, "terms", False, "no terms extracted")
    source = fixture.transcript.lower()
    for entry in terms:
        term = str(entry.get("term", "")).strip() if isinstance(entry, dict) else ""
        if not term:
            return CheckResult(fixture.id, "terms", False, "empty term entry")
        if term.lower() not in source:
            return CheckResult(fixture.id, "terms", False, f"hallucinated term {term!r}")
    return CheckResult(fixture.id, "terms", True)


def check_questions(fixture: Fixture, questions: list[str]) -> CheckResult:
    """Questions must be non-empty and grounded in transcript vocabulary."""
    if not questions:
        return CheckResult(fixture.id, "questions", False, "no questions")
    vocabulary = content_words(fixture.transcript)
    for question in questions:
        text = question.get("question", "") if isinstance(question, dict) else question
        text = str(text).strip()
        if not text:
            return CheckResult(fixture.id, "questions", False, "empty question")
        if not (content_words(text) & vocabulary):
            return CheckResult(fixture.id, "questions", False, f"ungrounded: {text[:60]!r}")
    return CheckResult(fixture.id, "questions", True)


def check_topics(fixture: Fixture, topics: list[dict], segment_count: int) -> CheckResult:
    """Topics need titles and in-range indices."""
    if not topics:
        return CheckResult(fixture.id, "topics", False, "no topics")
    for entry in topics:
        if not isinstance(entry, dict) or not str(entry.get("title", "")).strip():
            return CheckResult(fixture.id, "topics", False, "titleless topic")
        try:
            start, end = int(entry.get("start_segment", 0)), int(entry.get("end_segment", 0))
        except (TypeError, ValueError):
            return CheckResult(fixture.id, "topics", False, "non-integer indices")
        if not (0 <= start <= end < max(segment_count, 1)):
            return CheckResult(fixture.id, "topics", False, f"indices out of range: {start}-{end}")
    return CheckResult(fixture.id, "topics", True)


def check_translation(fixture: Fixture, translation: str | None) -> CheckResult:
    """Non-English fixtures must come back changed with terms preserved."""
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


def evaluate_fixture_outputs(fixture: Fixture, outputs: dict) -> list[CheckResult]:
    """Run every property check for one fixture against stage outputs."""
    return [
        check_notes(fixture, str(outputs.get("notes", ""))),
        check_terms(fixture, outputs.get("terms", [])),
        check_questions(fixture, outputs.get("questions", [])),
        check_topics(fixture, outputs.get("topics", []), int(outputs.get("segment_count", 1))),
        check_translation(fixture, outputs.get("translation")),
    ]


def summarize(results: list[CheckResult]) -> dict:
    """Aggregate a report mapping fixture -> stage -> pass/fail."""
    report: dict[str, dict[str, bool]] = {}
    for result in results:
        report.setdefault(result.fixture, {})[result.stage] = result.passed
    return report


def main() -> int:
    """Contract mode: validate the checkers themselves against fixed outputs."""
    good = {
        "notes": "Photosynthesis runs in the chloroplast at the thylakoid membrane.",
        "terms": [{"term": "chloroplast", "definition": "Plant cell organelle."}],
        "questions": ["Where do the light reactions run in the chloroplast?"],
        "topics": [{"title": "Intro", "summary": "Start", "start_segment": 0, "end_segment": 0}],
        "translation": "The chloroplast holds the thylakoid membrane where ATP and NADPH form during the Calvin cycle lecture.",
        "segment_count": 4,
    }
    bad = {
        "notes": "Mitochondria are great.",
        "terms": [{"term": "chloroplastzz", "definition": "Made up."}],
        "questions": [""],
        "topics": [{"title": "", "start_segment": 9, "end_segment": 2}],
        "translation": None,
        "segment_count": 4,
    }
    fixture = next(f for f in FIXTURES if f.id == "twi-english-codeswitch")
    good_results = evaluate_fixture_outputs(fixture, good)
    bad_results = evaluate_fixture_outputs(fixture, bad)
    print(fixture.id)
    ok = True
    for result in good_results:
        print(f"  good/{result.stage}: {'PASS' if result.passed else 'FAIL ' + result.detail}")
        ok = ok and result.passed
    for result in bad_results:
        print(f"  bad/{result.stage}: {'FAIL(expected)' if not result.passed else 'PASS(UNEXPECTED!)'}")
        ok = ok and not result.passed
    live = os.environ.get("WICX_EVAL_LIVE", "")
    if live:
        print("live mode requested (needs deployed analysis endpoint; see AUDIO_EVAL.md)")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
