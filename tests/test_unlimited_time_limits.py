import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from bot.engine_bundle import ensure_engine_root


class UnlimitedTimeLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine_root = ensure_engine_root()
        cls.harness_path = engine_root / "deobf" / "harness.py"
        cls.envlog_path = engine_root / "deobf" / "envlog.luau"

        spec = importlib.util.spec_from_file_location(
            "bundled_deobf_harness", cls.harness_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the bundled deobfuscator harness.")
        cls.harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.harness)

    def test_zero_disables_harness_timeout(self):
        output = self.harness._communicate(
            [
                sys.executable,
                "-c",
                "import time; print('started', flush=True); "
                "time.sleep(0.25); print('finished', flush=True)",
            ],
            timeout=0,
            stall=None,
        )

        self.assertIn(b"finished", output[0])
        self.assertEqual(output[-1], 0)

    def test_positive_harness_timeout_still_applies(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.harness._communicate(
                [sys.executable, "-c", "import time; time.sleep(1)"],
                timeout=0.05,
                stall=None,
            )

    def test_zero_disables_luau_trace_budget(self):
        source = self.envlog_path.read_text(encoding="utf-8")
        self.assertIn(
            "if TIME_BUDGET > 0 and now - START > TIME_BUDGET then",
            source,
        )

    def test_zero_disables_persistent_harness_deadline(self):
        source = self.harness_path.read_text(encoding="utf-8")
        self.assertIn(
            "self.deadline = time.time() + timeout if timeout > 0 else None",
            source,
        )


if __name__ == "__main__":
    unittest.main()