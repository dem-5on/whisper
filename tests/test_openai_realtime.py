from __future__ import annotations

import unittest

from transcriber.openai_realtime import OpenAIRealtimeCallbacks, OpenAIRealtimeEngine, resample_pcm16_16k_to_24k


class OpenAIRealtimeEngineTests(unittest.TestCase):
    def test_resampler_produces_24khz_duration(self) -> None:
        source = bytes(3200)  # 100 ms of 16 kHz mono PCM16
        self.assertEqual(len(resample_pcm16_16k_to_24k(source)), 4800)

    def test_session_update_uses_transcription_mode_and_pcm24(self) -> None:
        engine = OpenAIRealtimeEngine("gpt-live-transcribe", "en", OpenAIRealtimeCallbacks(lambda *_: None, lambda *_: None))
        session = engine._session_update()
        self.assertEqual(session["type"], "session.update")
        audio_input = session["session"]["audio"]["input"]
        self.assertEqual(audio_input["format"], {"type": "audio/pcm", "rate": 24000})
        self.assertEqual(audio_input["transcription"]["model"], "gpt-live-transcribe")
        self.assertEqual(audio_input["transcription"]["languages"], ["en"])
        self.assertIsNone(audio_input["turn_detection"])

    def test_frame_submission_is_queued(self) -> None:
        frame = bytes(640)
        engine = OpenAIRealtimeEngine("gpt-live-transcribe", None, OpenAIRealtimeCallbacks(lambda *_: None, lambda *_: None))
        engine.submit_audio(frame)
        self.assertEqual(engine._audio.get_nowait(), frame)
