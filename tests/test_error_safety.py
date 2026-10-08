from __future__ import annotations

import unittest

from transcriber.daemon import _safe_error


class ErrorSafetyTests(unittest.TestCase):
    def test_secret_like_error_values_are_redacted(self) -> None:
        self.assertNotIn("top-secret", _safe_error(RuntimeError("Authorization: Bearer top-secret")))
        self.assertIn("[redacted]", _safe_error(RuntimeError("api_key=top-secret")))
