from __future__ import annotations

import unittest
from unittest.mock import Mock

from whisper_service.hardware import HardwareDetectionError, detect_hardware


class HardwareDetectionTests(unittest.TestCase):
    def test_cpu_mode_does_not_probe_cuda(self) -> None:
        runtime = Mock()
        result = detect_hardware("cpu", ctranslate2_module=runtime)
        self.assertEqual(result.device, "cpu")
        self.assertEqual(result.compute_type, "int8")
        runtime.get_cuda_device_count.assert_not_called()

    def test_auto_selects_cuda_when_runtime_and_device_are_usable(self) -> None:
        runtime = Mock()
        runtime.get_cuda_device_count.return_value = 1
        runtime.get_supported_compute_types.side_effect = lambda device: {
            "cuda": {"float16", "int8_float16"}, "cpu": {"int8", "float32"}
        }[device]
        result = detect_hardware("auto", ctranslate2_module=runtime)
        self.assertEqual((result.device, result.compute_type, result.cuda_device_count), ("cuda", "float16", 1))

    def test_auto_falls_back_to_cpu_and_explains_why(self) -> None:
        runtime = Mock()
        runtime.get_cuda_device_count.return_value = 0
        runtime.get_supported_compute_types.return_value = {"int8", "float32"}
        result = detect_hardware("auto", ctranslate2_module=runtime)
        self.assertEqual(result.device, "cpu")
        self.assertIn("No visible CUDA devices", result.reason)

    def test_forced_cuda_fails_instead_of_silently_falling_back(self) -> None:
        runtime = Mock()
        runtime.get_cuda_device_count.return_value = 0
        with self.assertRaisesRegex(HardwareDetectionError, "CUDA was requested"):
            detect_hardware("cuda", ctranslate2_module=runtime)

    def test_runtime_exception_falls_back_in_auto_mode(self) -> None:
        runtime = Mock()
        runtime.get_cuda_device_count.side_effect = RuntimeError("driver details")
        runtime.get_supported_compute_types.return_value = {"int8", "float32"}
        result = detect_hardware("auto", ctranslate2_module=runtime)
        self.assertEqual(result.device, "cpu")
        self.assertIn("RuntimeError", result.reason)
        self.assertNotIn("driver details", result.reason)

    def test_unknown_device_mode_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            detect_hardware("metal")


if __name__ == "__main__":
    unittest.main()
