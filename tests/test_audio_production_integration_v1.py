from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps"))
sys.path.insert(0, str(ROOT / "scripts/04_media_archive_app"))

from media_archive_image_video_ui.pipeline_orchestrator import build_stage_plan  # noqa: E402
from media_archive_image_video_ui.search_jobs import SearchJobManager  # noqa: E402
from media_archive_image_video_ui.runtime_contract import (  # noqa: E402
    load_runtime_contract,
    task_runtime_from_contract,
)
from run_audio_enrichment_from_database_v1 import clean_temporary_audio  # noqa: E402


class AudioProductionIntegrationTests(unittest.TestCase):
    def _task(self, root: Path, mode: str) -> dict[str, object]:
        contract = load_runtime_contract(ROOT / "configs/media_archive_app_runtime_contract_v1.json")
        return {
            "task_id": "audio-fixture", "name": "audio fixture", "mode": mode,
            "workspace": str(root / "workspace"),
            "stage_output_root": str(root / "stages"),
            "database": str(root / "workspace/media_archive.sqlite"),
            "source_root": str(root / "source"),
            "profile": {
                "scheduler": {"model_workers": 1, "frame_extract_workers": 1},
                "video_sampling": {"frame_interval_seconds": 3},
                "high_value_policy": {"mode": "frozen_v25_compatible"},
            },
            "runtime": task_runtime_from_contract(
                contract, ocr_workers=1, embedding_workers=1,
                requested_scheduler_mode="stage_serial",
            ),
        }

    def test_audio_mode_is_one_isolated_maintenance_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = build_stage_plan(self._task(Path(temp), "audio_enrichment"))
        self.assertEqual([stage["key"] for stage in plan], ["audio_search_enrichment"])
        command = plan[0]["command"]
        self.assertIn("--confirm-central-db-write", command)
        self.assertIn("run_audio_enrichment_from_database_v1.py", " ".join(command))
        self.assertIn("--workers", command)

    def test_audio_filter_keeps_video_scope_and_requires_speech_evidence(self) -> None:
        manager = SearchJobManager(
            db_path=Path("/tmp/library.sqlite"), output_root=Path("/tmp/search"),
            search_script=Path("/tmp/search.py"), search_config=Path("/tmp/search.json"),
            embedding_python=Path("/tmp/text-python"),
            openclip_python=Path("/tmp/visual-python"),
        )
        command = manager.build_command(
            "收割机", {"media_type": "audio", "limit": 30}, Path("/tmp/out")
        )
        self.assertIn("--audio-evidence-only", command)
        media_index = command.index("--media-type")
        self.assertEqual(command[media_index + 1], "video")

    def test_new_full_task_appends_audio_as_stage_20(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            plan = build_stage_plan(self._task(Path(temp), "full"))
        self.assertEqual(plan[-1]["key"], "audio_search_enrichment")
        self.assertIn("20_audio_search_enrichment", " ".join(plan[-1]["command"]))

    def test_migration_and_temporary_audio_retention_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "media_archive.sqlite"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE source_assets(source_content_id TEXT PRIMARY KEY)")
                con.executescript((ROOT / "migrations/20260809_audio_speech_search_v1.sql").read_text())
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
            self.assertIn("audio_speech_evidence", tables)
            output = root / "item"
            (output / "deepfilter").mkdir(parents=True)
            (output / "audio_16k_mono.wav").write_bytes(b"temporary")
            (output / "audio_48k_mono.wav").write_bytes(b"temporary")
            (output / "deepfilter/enhanced.wav").write_bytes(b"temporary")
            (output / "audio_search_pilot.json").write_text(json.dumps({"text": "保留"}))
            clean_temporary_audio(output)
            self.assertFalse(any(output.rglob("*.wav")))
            self.assertTrue((output / "audio_search_pilot.json").is_file())

    def test_database_backfill_is_idempotent_and_keeps_no_audio_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            videos = []
            for index in range(3):
                video = source / f"fixture-{index}.mp4"
                video.write_bytes(f"read-only-source-fixture-{index}".encode())
                videos.append(video)
            source_identity = [
                (video.stat().st_size, video.stat().st_mtime_ns, video.read_bytes())
                for video in videos
            ]
            workspace = root / "workspace"
            workspace.mkdir()
            db = workspace / "media_archive.sqlite"
            with sqlite3.connect(db) as con:
                con.execute(
                    """CREATE TABLE source_assets(
                           source_content_id TEXT PRIMARY KEY,absolute_path TEXT,
                           relative_path TEXT,media_type TEXT,is_deleted_or_missing INTEGER
                       )"""
                )
                con.executemany(
                    "INSERT INTO source_assets VALUES(?,?,?,?,0)",
                    [
                        (f"video-{index}", str(video), video.name, "video")
                        for index, video in enumerate(videos)
                    ],
                )
                con.commit()
            pilot = root / "fake_pilot.py"
            pilot.write_text(
                "import argparse,json,os,time\n"
                "from pathlib import Path\n"
                "def main():\n"
                " p=argparse.ArgumentParser(); p.add_argument('--video'); p.add_argument('--source-root'); p.add_argument('--source-content-id'); p.add_argument('--output-dir'); p.add_argument('--ffmpeg'); p.add_argument('--ffprobe'); p.add_argument('--silero-root'); p.add_argument('--whisper-model'); p.add_argument('--deep-filter-executable'); p.add_argument('--deep-filter-model'); p.add_argument('--enhancement-failure-policy'); p.add_argument('--allow-no-audio',action='store_true'); a=p.parse_args(); o=Path(a.output_dir); o.mkdir(parents=True,exist_ok=True); events=o.parents[1]/'parallel_events.jsonl'; h=events.open('a'); h.write(json.dumps({'pid':os.getpid(),'event':'start','at':time.time()})+'\\n'); h.close(); time.sleep(.25); (o/'audio_16k_mono.wav').write_bytes(b'wav'); (o/'deepfilter').mkdir(exist_ok=True); (o/'deepfilter/enhanced.wav').write_bytes(b'wav'); c=o/'pilot_calls.txt'; c.write_text(str(int(c.read_text())+1) if c.exists() else '1'); report={'audio_stream_count':1,'search_evidence':[{'start_time_ms':1000,'end_time_ms':3000,'hit_time_ms':2000,'text':'麦子成熟了','language':'zh','preview_windows':[]} ]}; (o/'audio_search_pilot.json').write_text(json.dumps(report,ensure_ascii=False)); h=events.open('a'); h.write(json.dumps({'pid':os.getpid(),'event':'end','at':time.time()})+'\\n'); h.close(); return 0\n"
                "if __name__=='__main__': raise SystemExit(main())\n"
            )
            embedding = root / "fake_embedding.py"
            embedding.write_text(
                "import argparse,hashlib,sqlite3,struct\n"
                "p=argparse.ArgumentParser(); p.add_argument('--db'); p.add_argument('--model'); p.add_argument('--confirm-central-db-write',action='store_true'); a=p.parse_args(); con=sqlite3.connect(a.db); rows=con.execute('SELECT evidence_id FROM audio_speech_evidence').fetchall(); blob=struct.pack('<2f',1.0,0.0)\n"
                "for (eid,) in rows: con.execute(\"INSERT OR REPLACE INTO audio_text_embeddings VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)\",(eid,'fixture',a.model,2,'float32',1,blob,hashlib.sha256(blob).hexdigest(),'success'))\n"
                "con.commit(); con.close()\n"
            )
            model_dir = root / "model"
            model_dir.mkdir()
            out = workspace / "stages/20_audio_search_enrichment"
            command = [
                sys.executable,
                str(ROOT / "scripts/04_media_archive_app/run_audio_enrichment_from_database_v1.py"),
                "--db", str(db), "--out", str(out),
                "--migration", str(ROOT / "migrations/20260809_audio_speech_search_v1.sql"),
                "--embedding-python", sys.executable,
                "--audio-pilot-script", str(pilot), "--embedding-script", str(embedding),
                "--ffmpeg", sys.executable, "--ffprobe", sys.executable,
                "--silero-root", str(model_dir), "--whisper-model", str(model_dir),
                "--deep-filter-executable", sys.executable,
                "--deep-filter-model", str(model_dir), "--embedding-model", str(model_dir),
                "--workers", "3",
                "--confirm-central-db-write",
            ]
            first = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            second = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            with sqlite3.connect(db) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM audio_speech_evidence").fetchone()[0], 3)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM audio_text_embeddings").fetchone()[0], 3)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM audio_processing_items WHERE status='success'").fetchone()[0], 3)
            self.assertEqual([path.read_text() for path in out.rglob("pilot_calls.txt")], ["1"] * 3)
            self.assertFalse(any(out.rglob("*.wav")))
            self.assertEqual(
                [
                    (video.stat().st_size, video.stat().st_mtime_ns, video.read_bytes())
                    for video in videos
                ],
                source_identity,
            )
            events = [json.loads(line) for line in (out / "parallel_events.jsonl").read_text().splitlines()]
            starts = {row["pid"]: row["at"] for row in events if row["event"] == "start"}
            ends = {row["pid"]: row["at"] for row in events if row["event"] == "end"}
            self.assertEqual(len(starts), 3)
            self.assertTrue(any(
                starts[right] < ends[left]
                for left in starts for right in starts if left != right
            ))


if __name__ == "__main__":
    unittest.main()
