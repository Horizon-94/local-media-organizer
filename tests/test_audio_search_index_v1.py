import sqlite3
import tempfile
import unittest
from pathlib import Path

from apps.media_archive_image_video_ui.audio_search_index import (
    database_checks,
    initialize_database,
    search_database,
    upsert_evidence_and_vector,
)


class AudioSearchIndexTests(unittest.TestCase):
    def test_readonly_hybrid_search_returns_traceable_timeline(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "audio.sqlite"
            initialize_database(db)
            with sqlite3.connect(db) as con:
                upsert_evidence_and_vector(
                    con,
                    evidence_id="speech-1",
                    source_content_id="video-1",
                    source_path="/read/only/video.mp4",
                    start_time_ms=2920,
                    end_time_ms=6640,
                    hit_time_ms=4780,
                    transcript_text="麦子已经成熟了，今天可以开始收割。",
                    language="zh",
                    preview_windows={"5000": {"start_time_ms": 2780, "end_time_ms": 7780}},
                    model_name="fixture",
                    vector=[1.0, 0.0],
                )
                upsert_evidence_and_vector(
                    con,
                    evidence_id="fixture-2",
                    source_content_id="fixture-2",
                    source_path=None,
                    start_time_ms=0,
                    end_time_ms=1,
                    hit_time_ms=0,
                    transcript_text="海边岩石",
                    language="zh",
                    preview_windows={},
                    model_name="fixture",
                    vector=[0.0, 1.0],
                    is_fixture=True,
                )
                con.commit()
            result = search_database(db, query="收割", query_vector=[1.0, 0.0])
            self.assertEqual(result[0]["evidence_id"], "speech-1")
            self.assertEqual(result[0]["hit_time_ms"], 4780)
            self.assertEqual(result[0]["preview_windows"]["5000"]["start_time_ms"], 2780)
            checks = database_checks(db)
            self.assertEqual(checks["integrity_check"], "ok")
            self.assertEqual(checks["foreign_key_error_count"], 0)


if __name__ == "__main__":
    unittest.main()
