from __future__ import annotations

import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from whisper_service.env_file import (
    DEFAULT_ENV_FILE,
    ENV_FILE_VARIABLE,
    default_env_file,
    load_env_file,
    parse_env_text,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],  # noqa: S603 - fixed argv, test-only
        capture_output=True, text=True, check=False,
    )


class EnvFileParsingTests(unittest.TestCase):
    def test_parses_values_comments_and_quotes(self) -> None:
        text = """
# comment line
WHISPER_ENABLE_ANALYSIS=1

WHISPER_ANALYSIS_MODELS="openai/gpt-4o-mini,anthropic/claude-3.5-sonnet"
WHISPER_OPENROUTER_API_KEY='sk-or-secret'
not-a-pair
lowercase_key=value
WHISPER_EMPTY=
WHISPER_EQUALS=a=b
"""
        parsed = parse_env_text(text)
        self.assertEqual(parsed["WHISPER_ENABLE_ANALYSIS"], "1")
        self.assertEqual(parsed["WHISPER_ANALYSIS_MODELS"], "openai/gpt-4o-mini,anthropic/claude-3.5-sonnet")
        self.assertEqual(parsed["WHISPER_OPENROUTER_API_KEY"], "sk-or-secret")
        self.assertEqual(parsed["WHISPER_EQUALS"], "a=b")
        self.assertNotIn("lowercase_key", parsed)
        self.assertNotIn("WHISPER_EMPTY", parsed)
        self.assertNotIn("not-a-pair", parsed)

    def test_load_applies_missing_values_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("WICX_TEST_EXISTING=from-file\nWICX_TEST_NEW=from-file\n")
            os.environ["WICX_TEST_EXISTING"] = "from-environment"
            os.environ.pop("WICX_TEST_NEW", None)
            try:
                os.chmod(env_file, 0o600)
                applied = load_env_file(env_file)
                self.assertEqual(applied, {"WICX_TEST_NEW": "from-file"})
                self.assertEqual(os.environ["WICX_TEST_EXISTING"], "from-environment")
                self.assertEqual(os.environ["WICX_TEST_NEW"], "from-file")
            finally:
                os.environ.pop("WICX_TEST_EXISTING", None)
                os.environ.pop("WICX_TEST_NEW", None)

    def test_missing_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_env_file(Path(directory) / "absent.env"), {})

    def test_loose_permissions_warn_but_still_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("WICX_TEST_LOOSE=1\n")
            os.environ.pop("WICX_TEST_LOOSE", None)
            try:
                os.chmod(env_file, 0o644)
                with self.assertLogs("whisper_service.env", level=logging.WARNING) as logs:
                    applied = load_env_file(env_file)
                self.assertEqual(applied, {"WICX_TEST_LOOSE": "1"})
                self.assertTrue(any("chmod 600" in message for message in logs.output))
            finally:
                os.environ.pop("WICX_TEST_LOOSE", None)

    def test_values_never_reach_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("WICX_TEST_SECRET=super-secret-value\n")
            os.environ.pop("WICX_TEST_SECRET", None)
            try:
                os.chmod(env_file, 0o600)
                with self.assertLogs("whisper_service.env", level=logging.DEBUG) as logs:
                    load_env_file(env_file)
                joined = "\n".join(logs.output)
                self.assertIn("WICX_TEST_SECRET", joined)  # the name is fine
                self.assertNotIn("super-secret-value", joined)  # the value never is
            finally:
                os.environ.pop("WICX_TEST_SECRET", None)

    def test_default_path_honours_the_override_variable(self) -> None:
        previous = os.environ.get(ENV_FILE_VARIABLE)
        try:
            os.environ.pop(ENV_FILE_VARIABLE, None)
            self.assertEqual(default_env_file(), Path.cwd() / DEFAULT_ENV_FILE)
            os.environ[ENV_FILE_VARIABLE] = "/etc/wicx/service.env"
            self.assertEqual(default_env_file(), Path("/etc/wicx/service.env"))
        finally:
            if previous is None:
                os.environ.pop(ENV_FILE_VARIABLE, None)
            else:
                os.environ[ENV_FILE_VARIABLE] = previous


class EnvFileIsWiredBeforeArgumentDefaultsTests(unittest.TestCase):
    """The env file must be applied before argparse evaluates any default."""

    def _run(self, directory: Path, argv: list[str], extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("WHISPER_", "WICX_"))}
        environment.update(extra_env)
        code = (
            "import sys; sys.argv=['whisper-server', *sys.argv[1:]]; "
            "from whisper_service.cli import main; main()"
        )
        return subprocess.run(  # noqa: S603 - fixed argv, test-only
            [sys.executable, "-c", code, *argv],
            cwd=str(directory), env=environment, capture_output=True, text=True, check=False,
        )

    def test_env_file_supplies_a_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            entitlements = workspace / "from-file.json"
            (workspace / DEFAULT_ENV_FILE).write_text(
                f"WHISPER_ENTITLEMENTS_FILE={entitlements}\n", encoding="utf-8",
            )
            os.chmod(workspace / DEFAULT_ENV_FILE, 0o600)
            result = self._run(
                workspace,
                ["entitlement", "grant", "--user", "user-a", "--feature", "diarization"],
                {},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(entitlements.exists(), "the .env default was ignored")

    def test_real_environment_still_wins_over_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            from_file, from_env = workspace / "from-file.json", workspace / "from-env.json"
            (workspace / DEFAULT_ENV_FILE).write_text(
                f"WHISPER_ENTITLEMENTS_FILE={from_file}\n", encoding="utf-8",
            )
            os.chmod(workspace / DEFAULT_ENV_FILE, 0o600)
            result = self._run(
                workspace,
                ["entitlement", "grant", "--user", "user-a", "--feature", "diarization"],
                {"WHISPER_ENTITLEMENTS_FILE": str(from_env)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(from_env.exists())
            self.assertFalse(from_file.exists())


class SecretsStayOutOfGitTests(unittest.TestCase):
    """A committed env file is the classic way credentials leak."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tracked = git("ls-files").stdout.split()
        if not cls.tracked:  # not a git checkout; nothing to enforce
            raise unittest.SkipTest("repository metadata unavailable")

    def test_no_secret_env_file_is_tracked(self) -> None:
        offenders = [
            path for path in self.tracked
            if Path(path).name.startswith(".env") and Path(path).name != ".env.example"
        ]
        self.assertEqual(offenders, [])
        self.assertNotIn("keys.env", self.tracked)

    def test_example_and_ignore_rules_are_in_place(self) -> None:
        self.assertIn("deploy/whisper-service/.env.example", self.tracked)
        ignore_rules = (REPO_ROOT / ".gitignore").read_text().splitlines()
        stripped = {rule.strip() for rule in ignore_rules}
        self.assertIn(".env", stripped)
        for candidate in (".env", ".env", "deploy/whisper-service/.env", ".env.production"):
            result = git("check-ignore", "-q", candidate)
            self.assertEqual(result.returncode, 0, f"{candidate} should be git-ignored")
        for allowed in (".env.example", "deploy/whisper-service/.env.example"):
            self.assertEqual(
                git("check-ignore", "-q", allowed).returncode, 1,
                f"{allowed} must stay committable",
            )

    def test_example_documents_every_service_variable(self) -> None:
        example = (REPO_ROOT / "deploy/whisper-service/.env.example").read_text()
        source = "\n".join(
            path.read_text() for path in (REPO_ROOT / "whisper_service").glob("*.py")
        )
        import re

        declared = set(re.findall(r"os\.environ\.get\(\s*\"([A-Z0-9_]+)\"", source))
        declared |= set(re.findall(r"os\.environ\[\s*\"([A-Z0-9_]+)\"", source))
        declared |= {ENV_FILE_VARIABLE}
        # Desktop-only names are listed commented out on purpose.
        undocumented = {
            name for name in declared
            if name not in example and not name.startswith(("GROQ_", "OPENAI_", "OPENROUTER_", "TRANSCRIBER_", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "WAYLAND_", "XDG_"))
        }
        self.assertEqual(undocumented, set(), f"add these to .env.example: {sorted(undocumented)}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()