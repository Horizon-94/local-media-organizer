from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeExtractor:
    SAMPLING_OFFSET_MS = 2000
    SAMPLING_INTERVAL_MS = 3000
    SCRIPT_SCHEME = "frozen"
    SAMPLING_CONTRACT = {}
    SAMPLING_CONTRACT_ID = ""

    @classmethod
    def sampling_contract(cls):
        return {
            "scheme": cls.SCRIPT_SCHEME,
            "sampling_offset_ms": cls.SAMPLING_OFFSET_MS,
            "sampling_interval_ms": cls.SAMPLING_INTERVAL_MS,
        }


class GenericSamplingDensityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sampling = load(
            ROOT / "scripts/04_media_archive_app/step02_video_frame_generic_interval_v1.py",
            "generic_sampling_test",
        )
        cls.density = load(
            ROOT / "scripts/04_media_archive_app/stop03_2_candidate_queues_generic_library_v1.py",
            "generic_density_test",
        )

    def test_all_five_sampling_intervals_create_distinct_contracts(self) -> None:
        identifiers = set()
        for seconds in (1, 2, 3, 4, 5):
            contract = self.sampling.configure_sampling_contract(FakeExtractor, seconds)
            self.assertEqual(contract["sampling_interval_ms"], seconds * 1000)
            identifiers.add(FakeExtractor.SAMPLING_CONTRACT_ID)
        self.assertEqual(len(identifiers), 5)
        with self.assertRaises(ValueError):
            self.sampling.configure_sampling_contract(FakeExtractor, 10)

    def test_sampling_adapter_treats_frozen_none_return_as_success(self) -> None:
        class FrozenReturnsNone(FakeExtractor):
            __file__ = __file__

            @staticmethod
            def main():
                return None

        with mock.patch.object(
            self.sampling,
            "load_frozen_extractor",
            return_value=FrozenReturnsNone,
        ):
            exit_code = self.sampling.main(["--frame-interval-seconds", "3"])
        self.assertEqual(exit_code, 0)

    def test_sampling_adapter_propagates_runtime_output_root_to_late_import(self) -> None:
        class FrozenReturnsNone(FakeExtractor):
            __file__ = __file__
            TEST_OUTPUT_ROOT = Path("/historical/test-output")

            @staticmethod
            def main():
                return None

        with tempfile.TemporaryDirectory() as temp:
            selected_library = Path(temp).resolve()
            previous = getattr(self.sampling, "TEST_OUTPUT_ROOT", None)
            had_previous = hasattr(self.sampling, "TEST_OUTPUT_ROOT")
            self.sampling.TEST_OUTPUT_ROOT = selected_library
            try:
                with mock.patch.object(
                    self.sampling,
                    "load_frozen_extractor",
                    return_value=FrozenReturnsNone,
                ):
                    exit_code = self.sampling.main(
                        ["--frame-interval-seconds", "3"]
                    )
            finally:
                if had_previous:
                    self.sampling.TEST_OUTPUT_ROOT = previous
                else:
                    delattr(self.sampling, "TEST_OUTPUT_ROOT")
        self.assertEqual(exit_code, 0)
        self.assertEqual(FrozenReturnsNone.TEST_OUTPUT_ROOT, selected_library)

    def test_density_targets_are_dynamic_not_fixed_counts(self) -> None:
        self.assertEqual(self.density.density_target_count(10, 0.15), 2)
        self.assertEqual(self.density.density_target_count(10, 0.20), 2)
        self.assertEqual(self.density.density_target_count(10, 0.30), 3)
        self.assertEqual(self.density.density_target_count(100, 0.15), 15)
        self.assertEqual(self.density.density_target_count(100, 0.20), 20)
        self.assertEqual(self.density.density_target_count(100, 0.30), 30)
        self.assertEqual(self.density.density_target_count(137, 0.20), 28)

    def test_temporal_density_is_deterministic_and_spread(self) -> None:
        rows = [
            {
                "visual_unit_id": f"v{index}",
                "source_content_id": "source",
                "time_position_ms": index * 1000,
                "frame_index": index,
                "generic_high_signal": index in {2, 7},
                "generic_high_signal_score": index / 10,
                "grid_structure": 0.1,
            }
            for index in range(10)
        ]
        first = self.density.select_temporal_density_frames(rows, 0.30)
        second = self.density.select_temporal_density_frames(list(reversed(rows)), 0.30)
        self.assertEqual(
            [row["visual_unit_id"] for row in first],
            [row["visual_unit_id"] for row in second],
        )
        self.assertEqual(len(first), 3)
        self.assertEqual([row["visual_unit_id"] for row in first], ["v2", "v5", "v7"])

    def test_density_gate_rejects_a_count_mismatch(self) -> None:
        base = {
            "automatic_acceptance_gates": {"base": True},
            "input_video_visual_units": 10,
            "execution_mode": "dry-run",
        }
        passed = self.density.apply_gate_applicability(
            dict(base),
            {"target_ratio": 0.20, "target_count_matches": True},
        )
        self.assertEqual(passed["technical_status"], "PASS")
        failed = self.density.apply_gate_applicability(
            dict(base),
            {"target_ratio": 0.20, "target_count_matches": False},
        )
        self.assertEqual(failed["technical_status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
