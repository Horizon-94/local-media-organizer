import subprocess
import tempfile
import unittest
from pathlib import Path

from apps.media_archive_image_video_ui.audio_enhancement import (
    deepfilter_command,
    enhance_with_deepfilter,
    reusable_audio_pilot_report,
    source_stat_contract,
)


class DeepFilterEnhancementTests(unittest.TestCase):
    def test_command_uses_native_model_and_delay_compensation(self):
        command = deepfilter_command(
            executable=Path("/tool/deep-filter"),
            model=Path("/models/DeepFilterNet3_onnx.tar.gz"),
            input_wav=Path("/work/input.wav"),
            output_dir=Path("/work/out"),
        )
        self.assertEqual(command[0], "/tool/deep-filter")
        self.assertIn("--compensate-delay", command)
        self.assertIn("--output-dir", command)
        self.assertNotIn("--out-dir", command)
        self.assertEqual(command[-1], "/work/input.wav")

    def test_adapter_requires_exactly_one_new_nonempty_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "deep-filter"
            executable.write_bytes(b"binary")
            model = root / "DeepFilterNet3_onnx.tar.gz"
            model.write_bytes(b"model")
            source = root / "input.wav"
            source.write_bytes(b"0" * 100)
            output = root / "out"

            def fake_runner(command, **kwargs):
                output.mkdir(parents=True, exist_ok=True)
                (output / "input_DeepFilterNet3.wav").write_bytes(b"1" * 100)
                return subprocess.CompletedProcess(command, 0, "", "")

            result = enhance_with_deepfilter(
                executable=executable,
                model=model,
                input_wav=source,
                output_dir=output,
                runner=fake_runner,
            )
            self.assertEqual(result.backend, "deepfilternet3_native_arm64")
            self.assertTrue(Path(result.output_path).is_file())

    def test_adapter_rejects_missing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "deep-filter"
            executable.write_bytes(b"binary")
            model = root / "DeepFilterNet3_onnx.tar.gz"
            model.write_bytes(b"model")
            source = root / "input.wav"
            source.write_bytes(b"0" * 100)

            def fake_runner(command, **kwargs):
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                enhance_with_deepfilter(
                    executable=executable,
                    model=model,
                    input_wav=source,
                    output_dir=root / "out",
                    runner=fake_runner,
                )

    def test_adapter_accepts_deterministically_overwritten_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "deep-filter"
            executable.write_bytes(b"binary")
            model = root / "DeepFilterNet3_onnx.tar.gz"
            model.write_bytes(b"model")
            source = root / "input.wav"
            source.write_bytes(b"0" * 100)
            output = root / "out"
            output.mkdir()
            expected = output / "input_DeepFilterNet3.wav"
            expected.write_bytes(b"old" * 40)

            def fake_runner(command, **kwargs):
                expected.write_bytes(b"new" * 50)
                return subprocess.CompletedProcess(command, 0, "", "")

            result = enhance_with_deepfilter(
                executable=executable,
                model=model,
                input_wav=source,
                output_dir=output,
                runner=fake_runner,
            )
            self.assertEqual(Path(result.output_path), expected.resolve())

    def test_checkpoint_reuse_requires_same_source_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "clip.mov"
            video.write_bytes(b"video")
            executable = root / "deep-filter"
            executable.write_bytes(b"binary")
            model = root / "DeepFilterNet3_onnx.tar.gz"
            model.write_bytes(b"model")
            report = {
                "contract": "media_archive_audio_search_pilot_v2",
                "status": "PASS",
                "source_read_only": True,
                "source_video": str(video.resolve()),
                "source_stat": source_stat_contract(video),
                "processing_config": {
                    "deep_filter_executable": str(executable.resolve()),
                    "deep_filter_model": str(model.resolve()),
                    "enhancement_failure_policy": "fallback",
                },
            }
            self.assertTrue(reusable_audio_pilot_report(
                report,
                video=video,
                deep_filter_executable=executable,
                deep_filter_model=model,
                enhancement_failure_policy="fallback",
            ))
            video.write_bytes(b"changed")
            self.assertFalse(reusable_audio_pilot_report(
                report,
                video=video,
                deep_filter_executable=executable,
                deep_filter_model=model,
                enhancement_failure_policy="fallback",
            ))


if __name__ == "__main__":
    unittest.main()
