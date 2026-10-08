from __future__ import annotations

import unittest
from unittest.mock import patch

from transcriber.platforms.windows.paths import config_dir, data_dir, socket_path


class WindowsPathsTests(unittest.TestCase):
    def test_paths_use_per_user_windows_environment(self) -> None:
        with patch.dict("os.environ", {"APPDATA": r"C:\Users\Ada\Roaming", "LOCALAPPDATA": r"C:\Users\Ada\Local"}):
            self.assertEqual(str(config_dir()), r"C:\Users\Ada\Roaming/Whisper")
            self.assertEqual(str(data_dir()), r"C:\Users\Ada\Local/Whisper")

    def test_socket_lives_in_user_temp_directory(self) -> None:
        with patch("transcriber.platforms.windows.paths.tempfile.gettempdir", return_value=r"C:\Users\Ada\Temp"):
            self.assertEqual(str(socket_path()), r"C:\Users\Ada\Temp/whisper-daemon.sock")
