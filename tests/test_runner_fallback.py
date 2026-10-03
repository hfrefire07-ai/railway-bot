import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot.config import Settings
from bot.runner import DeobfuscationError, run_deobfuscator


class _FakeProcess:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.pid = 987654321
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


class FastFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.input_path = Path(self.tempdir.name) / "sample.luau"
        self.input_path.write_text("return 1\n", encoding="utf-8")
        self.settings = Settings(
            token="test",
            process_timeout_seconds=5,
            trace_budget_seconds=5,
        )

    async def asyncTearDown(self):
        self.tempdir.cleanup()

    async def test_sigkill_retries_fast_and_labels_partial_output(self):
        calls = []

        async def fake_create_subprocess_exec(*command, **kwargs):
            if "--no-devirt" in command:
                calls.append("fast")
                output_path = Path(command[command.index("--output") + 1])
                output_path.write_text("return 1\n", encoding="utf-8")
                return _FakeProcess(
                    0,
                    stderr=b"[*] obfuscator: Luraph v15\n",
                )
            calls.append("full")
            return _FakeProcess(
                -9,
                stderr=b"[*] obfuscator: Luraph v15\n[*] devirt round 1\n",
            )

        with (
            patch("bot.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec),
            patch("bot.runner._kill_process_tree") as kill_tree,
        ):
            result = await run_deobfuscator(
                self.input_path,
                "sample.luau",
                self.settings,
            )

        try:
            self.assertEqual(calls, ["full", "fast"])
            self.assertTrue(result.fallback_used)
            self.assertEqual(result.mode, "Fast fallback (partial)")
            self.assertEqual(result.detected_obfuscator, "Luraph v15")
            self.assertIn("SIGKILL", result.output_path.read_text(encoding="utf-8"))
            kill_tree.assert_called_once()
        finally:
            result.output_path.unlink(missing_ok=True)

    async def test_full_flag_behavior_can_disable_fallback(self):
        calls = []

        async def fake_create_subprocess_exec(*command, **kwargs):
            calls.append(command)
            return _FakeProcess(
                -9,
                stderr=b"[*] obfuscator: Luraph v15\n",
            )

        with (
            patch("bot.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec),
            patch("bot.runner._kill_process_tree"),
        ):
            with self.assertRaises(DeobfuscationError) as raised:
                await run_deobfuscator(
                    self.input_path,
                    "sample.luau",
                    self.settings,
                    allow_fast_fallback=False,
                )

        self.assertEqual(len(calls), 1)
        self.assertEqual(raised.exception.return_code, -9)
        self.assertIn("SIGKILL", str(raised.exception))
        if raised.exception.log_path:
            raised.exception.log_path.unlink(missing_ok=True)

    async def test_engine_trace_fallback_is_marked_partial(self):
        async def fake_create_subprocess_exec(*command, **kwargs):
            output_path = Path(command[command.index("--output") + 1])
            output_path.write_text("-- short behavior trace\n", encoding="utf-8")
            return _FakeProcess(
                0,
                stderr=(
                    b"[*] obfuscator: Luraph v15\n"
                    b"[!] devirtualization failed: RuntimeError: unsupported VM branch\n"
                    b"[!] devirtualization produced no output; writing the behaviour trace instead\n"
                ),
            )

        with patch(
            "bot.runner.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        ):
            result = await run_deobfuscator(
                self.input_path,
                "sample.luau",
                self.settings,
            )

        try:
            self.assertEqual(result.mode, "Behavior trace fallback (partial)")
            self.assertEqual(
                result.partial_reason,
                "Devirtualization failed: RuntimeError: unsupported VM branch",
            )
            self.assertIn(
                "partial behavior trace",
                result.output_path.read_text(encoding="utf-8"),
            )
        finally:
            result.output_path.unlink(missing_ok=True)

    async def test_full_mode_rejects_engine_trace_fallback_with_log(self):
        async def fake_create_subprocess_exec(*command, **kwargs):
            output_path = Path(command[command.index("--output") + 1])
            output_path.write_text("-- short behavior trace\n", encoding="utf-8")
            return _FakeProcess(
                0,
                stderr=(
                    b"[*] obfuscator: Luraph v15\n"
                    b"[!] devirtualization produced no output; writing the behaviour trace instead\n"
                ),
            )

        with patch(
            "bot.runner.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        ):
            with self.assertRaises(DeobfuscationError) as raised:
                await run_deobfuscator(
                    self.input_path,
                    "sample.luau",
                    self.settings,
                    allow_fast_fallback=False,
                )

        self.assertIn("traza parcial", str(raised.exception))
        self.assertIsNotNone(raised.exception.log_path)
        self.assertIn(
            "devirtualization produced no output",
            raised.exception.log_path.read_text(encoding="utf-8"),
        )
        raised.exception.log_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()