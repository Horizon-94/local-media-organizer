from __future__ import annotations

import unittest

from apps.media_archive_image_video_ui.audio_search_timeline import (
    TranscriptSegment,
    classify_vad_timeline,
    preview_window_for_hit,
    transcript_search_evidence,
    vad_sample_intervals_to_ms,
    whisper_clip_timestamps,
)


class AudioSearchTimelineTests(unittest.TestCase):
    def test_audio_hit_uses_existing_visual_preview_contract(self) -> None:
        self.assertEqual(
            preview_window_for_hit(26_000, 10_000),
            {
                "start_time_ms": 24_000,
                "end_time_ms": 34_000,
                "hit_time_ms": 26_000,
                "requires_source_duration_clamp": False,
            },
        )
        self.assertEqual(preview_window_for_hit(1_000, 5_000)["start_time_ms"], 0)

    def test_transcript_keeps_exact_time_and_both_search_windows(self) -> None:
        evidence = transcript_search_evidence(TranscriptSegment(
            source_content_id="video-1",
            start_time_ms=24_500,
            end_time_ms=27_500,
            text="麦子已经成熟了",
            language="zh",
        ))
        self.assertEqual(evidence["hit_time_ms"], 26_000)
        self.assertEqual(evidence["preview_windows"]["5000"]["start_time_ms"], 24_000)
        self.assertEqual(evidence["preview_windows"]["10000"]["end_time_ms"], 34_000)
        self.assertTrue(evidence["embedding_required"])

    def test_vad_never_guesses_music_wind_or_environment(self) -> None:
        timeline = classify_vad_timeline(12_000, [
            {"start_time_ms": 2_000, "end_time_ms": 5_000},
            {"start_time_ms": 4_900, "end_time_ms": 7_000},
        ])
        self.assertEqual([row["audio_class"] for row in timeline], [
            "non_speech_unclassified", "speech", "non_speech_unclassified",
        ])
        self.assertEqual(timeline[1]["start_time_ms"], 2_000)
        self.assertEqual(timeline[1]["end_time_ms"], 7_000)

    def test_silero_samples_become_whisper_clip_timestamps(self) -> None:
        intervals = vad_sample_intervals_to_ms([
            {"start": 16_000, "end": 40_000},
            {"start": 64_000, "end": 80_000},
        ])
        self.assertEqual(intervals, [
            {"start_time_ms": 1_000, "end_time_ms": 2_500},
            {"start_time_ms": 4_000, "end_time_ms": 5_000},
        ])
        self.assertEqual(whisper_clip_timestamps(intervals), [1.0, 2.5, 4.0, 5.0])


if __name__ == "__main__":
    unittest.main()
