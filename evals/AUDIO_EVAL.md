# Speech-to-text evaluation protocol

No microphone audio ships with this repository. Measure before claiming:

1. Record `evals/clips/` yourself (16 kHz mono WAV, 5–60s, consented speakers):
   clean English, Twi-English code-switching, French-English mixing, a
   regional accent, fast speech, and acronym/jargon-heavy speech.
2. Write `evals/clips/references.json` with exact reference transcripts.
3. Run `python -m evals.audio_eval` and record per-language WER.

Lecture *analysis* quality (notes/topics/terms/questions/translation) is
covered separately by `evals/lecture_eval.py` fixtures and property checks;
run `python -m evals.lecture_eval` for the contract suite.
