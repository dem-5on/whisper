from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from transcriber.daemon import _schedule_server_shutdown
from transcriber.platforms.windows.paths import config_dir, data_dir, socket_path
from transcriber.platforms.windows import lifecycle


class WindowsPathsTests(unittest.TestCase):
    def test_paths_use_per_user_windows_environment(self) -> None:
        with patch.dict("os.environ", {"APPDATA": r"C:\Users\Ada\Roaming", "LOCALAPPDATA": r"C:\Users\Ada\Local"}):
            self.assertEqual(config_dir().name, "Whisper")
            self.assertTrue(str(config_dir().parent).endswith(r"Ada\Roaming"))
            self.assertEqual(data_dir().name, "Whisper")
            self.assertTrue(str(data_dir().parent).endswith(r"Ada\Local"))

    def test_socket_lives_in_user_temp_directory(self) -> None:
        with patch("transcriber.platforms.windows.paths.tempfile.gettempdir", return_value=r"C:\Users\Ada\Temp"):
            self.assertEqual(socket_path().name, "whisper-daemon.sock")
            self.assertTrue(str(socket_path().parent).endswith(r"Ada\Temp"))

    def test_shutdown_is_scheduled_off_the_request_thread(self) -> None:
        server = MagicMock()
        with patch("transcriber.daemon.threading.Thread") as thread:
            _schedule_server_shutdown(server)
        thread.assert_called_once_with(
            target=server.shutdown,
            name="transcriber-shutdown-request",
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_start_daemon_launches_hidden_child_and_waits_for_socket(self) -> None:
        with patch.object(lifecycle, "is_running", side_effect=(False, True)), patch.object(
            lifecycle.subprocess, "Popen"
        ) as popen:
            lifecycle.start_daemon()
        self.assertEqual(popen.call_args.args[0][1:], ["-m", "transcriber.daemon"])
        self.assertEqual(popen.call_args.kwargs["stdin"], lifecycle.subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stdout"], lifecycle.subprocess.DEVNULL)

    def test_stop_daemon_requests_shutdown_and_waits_for_exit(self) -> None:
        with patch.object(lifecycle, "is_running", side_effect=(True, False)), patch.object(
            lifecycle, "request", return_value={"ok": "true"}
        ) as request:
            lifecycle.stop_daemon()
        request.assert_called_once_with("shutdown")

    def test_restart_stops_before_starting(self) -> None:
        with patch.object(lifecycle, "stop_daemon") as stop, patch.object(lifecycle, "start_daemon") as start:
            lifecycle.restart_daemon()
        stop.assert_called_once_with()
        start.assert_called_once_with()
