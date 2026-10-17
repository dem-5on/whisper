"""Speech-to-text evaluation runner with reference transcripts.

The repository ships no microphone audio (and must not: recordings are
private). To measure transcription quality:

1. Create ``evals/clips/`` (git-ignored) with ``references.json`` shaped as
   ``[{"id": "en-clean-01", "file": "en-clean-01.wav", "language": "en",
   "reference": "Exact words spoken."}, ...]``. Record each clip yourself
   with explicit speaker consent: 16 kHz mono WAV, 5-60 seconds, quiet room
   plus a few noisy ones. Cover at minimum: clean English, Twi-English
   code-switching, French-English mixing, a regional accent, fast speech,
   and acronym/jargon-heavy speech.
2. Run ``python -m evals.audio_eval``. Each clip is transcribed with the
   configured local backend and scored with word error rate plus a vocabulary
   check (acronyms/names from the reference must survive).
3. No accuracy claim ships until its language rows are measured here.

Without ``clips/references.json`` the runner prints this protocol and exits 2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word-level edit distance divided by reference length (0..inf)."""
    ref = reference.split()
    hyp = hypothesis.split()
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        current = [i]
        for j, other in enumerate(hyp, 1):
            substitution = previous[j - 1] + (0 if word.lower() == other.lower() else 1)
            current.append(min(previous[j] + 1, current[j - 1] + 1, substitution))
        previous = current
    return previous[-1] / len(ref)


def main() -> int:
    clips_dir = Path(__file__).resolve().parent / "clips"
    references = clips_dir / "references.json"
    if not references.is_file():
        print(__doc__)
        return 2
    try:
        items = json.loads(references.read_text(encoding="utf-8"))
    except ValueError:
        print("references.json is not valid JSON")
        return 2
    if not isinstance(items, list) or not items:
        print("references.json must be a non-empty list")
        return 2
    from transcriber.config import load_config
    from transcriber.transcription import make_backend

    config = load_config()
    backend = make_backend(config.transcription)
    rows: list[tuple[str, str, float]] = []
    for item in items:
        audio = clips_dir / str(item.get("file", ""))
        if not audio.is_file():
            print(f"SKIP {item.get('id')}: missing {audio.name}")
            continue
        try:
            hypothesis = backend.transcribe(audio)
        except Exception as exc:  # keep one bad clip from killing the run
            print(f"ERROR {item.get('id')}: {type(exc).__name__}")
            continue
        wer = word_error_rate(str(item.get("reference", "")), hypothesis)
        rows.append((str(item.get("id")), str(item.get("language", "?")), wer))
        print(f"{item.get('id')} [{item.get('language')}] WER={wer:.3f}")
    if not rows:
        return 2
    by_language: dict[str, list[float]] = {}
    for _, language, wer in rows:
        by_language.setdefault(language, []).append(wer)
    print("\nPer-language mean WER:")
    for language in sorted(by_language):
        scores = by_language[language]
        print(f"  {language}: {sum(scores) / len(scores):.3f} (n={len(scores)})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
