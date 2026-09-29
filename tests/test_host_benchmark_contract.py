from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "tools/pipeline_benchmark/run_host_benchmark.sh"
SAFE_ROOT = Path("/Users/horizon/Documents/AI-Local/test-output")


class HostBenchmarkContractTest(unittest.TestCase):
    def test_script_uses_common_safe_root_and_separate_run_output(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('QWEN_ALLOWED_ROOT="${QWEN_ALLOWED_OUTPUT_ROOT:-/Users/horizon/Documents/AI-Local/test-output}"', text)
        self.assertIn('--allowed-output-root "$QWEN_ALLOWED_ROOT"', text)
        self.assertIn('--out "$OUT/qwen-stage"', text)
        fixture = SAFE_ROOT / "qwen-host-fixture-v2" / "derived"
        output = SAFE_ROOT / "qwen-x1-cold-N" / "qwen-stage"
        self.assertEqual(SAFE_ROOT, fixture.parents[1])
        self.assertEqual(SAFE_ROOT, output.parents[1])
        self.assertNotEqual(fixture.parent, output.parent)

    def test_preflight_only_shares_final_mode_arguments(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('if [[ "$PREFLIGHT_ONLY" == "1" ]]; then', text)
        self.assertIn('--mode "$MODE"', text)
        self.assertIn('--workers 1', text)
        self.assertIn('--max-tokens 384', text)
        self.assertIn('--max-attempts 1', text)


if __name__ == "__main__":
    unittest.main()
