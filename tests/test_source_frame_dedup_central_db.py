from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from PIL import Image


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "source_frame_dedup_central_db.py"
if not SCRIPT_PATH.exists():
    SCRIPT_PATH = Path(__file__).resolve().with_name("source_frame_dedup_central_db.py")
SPEC = importlib.util.spec_from_file_location("central_dedup", SCRIPT_PATH)
assert SPEC and SPEC.loader
central = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(central)


def make_image(path: Path, color: tuple[int, int, int], accent: bool = False) -> None:
    image = Image.new("RGB", (64, 48), color)
    if accent:
        for x in range(16, 48):
            for y in range(12, 36):
                image.putpixel((x, y), (255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "JPEG", quality=95)


def create_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    first = source_root / "a.jpg"
    duplicate = source_root / "nested" / "a-copy.jpg"
    duplicate.parent.mkdir()
    first.write_bytes(b"same-source-content")
    duplicate.write_bytes(first.read_bytes())
    video = source_root / "clip.mp4"
    video.write_bytes(b"unique-video-content")

    derived = tmp_path / "derived"
    frame1 = derived / "frame1.jpg"
    frame2 = derived / "frame2.jpg"
    frame3 = derived / "frame3.jpg"
    make_image(frame1, (40, 50, 60), accent=True)
    frame2.write_bytes(frame1.read_bytes())
    make_image(frame3, (200, 20, 30), accent=False)

    db = tmp_path / "media_archive.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE source_assets (
          source_content_id TEXT PRIMARY KEY, absolute_path TEXT NOT NULL UNIQUE, relative_path TEXT NOT NULL,
          file_name TEXT NOT NULL, extension TEXT NOT NULL, media_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          mtime INTEGER NOT NULL, ctime INTEGER NOT NULL, volume_id TEXT NOT NULL DEFAULT 'LOCAL', online_status INTEGER DEFAULT 1,
          first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,is_deleted_or_missing INTEGER DEFAULT 0
        );
        CREATE TABLE source_file_records (
          source_file_id TEXT PRIMARY KEY,source_content_id TEXT NOT NULL,absolute_path TEXT NOT NULL,relative_path TEXT NOT NULL,
          source_root TEXT NOT NULL,file_name TEXT NOT NULL,extension TEXT NOT NULL,media_kind TEXT NOT NULL,support_status TEXT NOT NULL,
          support_reason TEXT,size_bytes INTEGER,mtime_ns INTEGER,ctime_ns INTEGER,content_sha256 TEXT,dedup_role TEXT,next_action TEXT,
          canonical_source_file_id TEXT,folder_path TEXT,file_stem TEXT,stem_key TEXT,finder_tag_status TEXT,finder_tags_json TEXT,
          scan_run_id TEXT NOT NULL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE derived_assets (
          derived_id TEXT PRIMARY KEY,source_content_id TEXT NOT NULL,derived_type TEXT NOT NULL,derived_path TEXT NOT NULL UNIQUE,
          frame_index INTEGER DEFAULT -1,time_position_ms INTEGER DEFAULT -1,width INTEGER,height INTEGER,sha256 TEXT,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id)
        );
        CREATE TABLE visual_units (
          visual_unit_id TEXT PRIMARY KEY,source_content_id TEXT NOT NULL,derived_id TEXT NOT NULL,visual_file TEXT NOT NULL,
          time_position_ms INTEGER NOT NULL DEFAULT -1,near_black INTEGER DEFAULT 0,luma_mean REAL,luma_std REAL,
          near_dup_group_id TEXT,is_near_dup_representative INTEGER DEFAULT 0,
          FOREIGN KEY(source_content_id) REFERENCES source_assets(source_content_id),FOREIGN KEY(derived_id) REFERENCES derived_assets(derived_id)
        );
        CREATE TABLE model_runs (
          run_id TEXT PRIMARY KEY,stage TEXT NOT NULL,model_name TEXT NOT NULL,model_path TEXT NOT NULL,script_version TEXT NOT NULL,
          script_path TEXT,input_count INTEGER NOT NULL DEFAULT 0,output_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,
          started_at TEXT NOT NULL,finished_at TEXT,error_message TEXT
        );
        CREATE TABLE embeddings (
          embedding_id TEXT PRIMARY KEY,visual_unit_id TEXT NOT NULL,source_content_id TEXT NOT NULL,model_name TEXT NOT NULL,
          model_path TEXT NOT NULL,dimension INTEGER NOT NULL DEFAULT 4,vector_key TEXT NOT NULL,run_id TEXT NOT NULL,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    source_rows = [
        ("sc_a", first, "a.jpg", "image"),
        ("sc_b", duplicate, "nested/a-copy.jpg", "image"),
        ("sc_v", video, "clip.mp4", "video"),
    ]
    for index, (content_id, path, relative, media) in enumerate(source_rows, 1):
        stat = path.stat()
        con.execute(
            "INSERT INTO source_assets(source_content_id,absolute_path,relative_path,file_name,extension,media_type,size_bytes,mtime,ctime) VALUES(?,?,?,?,?,?,?,?,?)",
            (content_id, str(path), relative, path.name, path.suffix, media, stat.st_size, int(stat.st_mtime), int(stat.st_ctime)),
        )
        con.execute(
            """INSERT INTO source_file_records
            (source_file_id,source_content_id,absolute_path,relative_path,source_root,file_name,extension,media_kind,support_status,
             size_bytes,mtime_ns,ctime_ns,content_sha256,dedup_role,next_action,scan_run_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"sf_{index}", content_id, str(path), relative, str(source_root), path.name, path.suffix, media, "supported",
             stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, "", "canonical", "process", "scan_fixture"),
        )
    for index, path in enumerate((frame1, frame2, frame3), 1):
        derived_id = f"da_{index}"
        visual_id = f"vu_{index}"
        con.execute(
            "INSERT INTO derived_assets(derived_id,source_content_id,derived_type,derived_path,frame_index,time_position_ms,sha256) VALUES(?,?,?,?,?,?,?)",
            (derived_id, "sc_v", "video_frame", str(path), index, index * 1000, f"derived-hash-{index}"),
        )
        con.execute(
            "INSERT INTO visual_units(visual_unit_id,source_content_id,derived_id,visual_file,time_position_ms) VALUES(?,?,?,?,?)",
            (visual_id, "sc_v", derived_id, str(path), index * 1000),
        )
    vector_path = tmp_path / "openclip_vectors.jsonl"
    vectors = {
        "vu_1": [1.0, 0.0, 0.0, 0.0],
        "vu_2": [1.0, 0.0, 0.0, 0.0],
        "vu_3": [0.0, 1.0, 0.0, 0.0],
    }
    vector_path.write_text("".join(json.dumps({"visual_unit_id": key, "vector": value}) + "\n" for key, value in vectors.items()), encoding="utf-8")
    for index, visual_id in enumerate(vectors, 1):
        con.execute(
            "INSERT INTO embeddings(embedding_id,visual_unit_id,source_content_id,model_name,model_path,dimension,vector_key,run_id) VALUES(?,?,?,?,?,?,?,?)",
            (f"emb_{index}", visual_id, "sc_v", "ViT-B-32", "local", 4, f"jsonl:{vector_path}#emb_{index}", "emb_fixture"),
        )
    con.commit()
    con.close()
    return db, source_root, tmp_path / "test-output"


def args_for(db: Path, output: Path, mode: str, **overrides) -> argparse.Namespace:
    values = {
        "db": str(db),
        "output_root": str(output),
        "run_id": "fixture_run",
        "mode": mode,
        "max_workers": 20,
        "quick_bytes": 8,
        "visual_hash_threshold": 6,
        "openclip_cosine_threshold": 0.98,
        "video_time_window_sec": 30,
        "decode_backend": "pillow",
        "determinism_check": True,
        "force_commit_review": False,
        "inject_failure_before_commit": False,
        "expected_source_rows": 0,
        "expected_source_groups": 0,
        "expected_visual_rows": 0,
        "expected_visual_groups": 0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def fixture_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db, source_root, test_output = create_fixture(tmp_path)
    test_output.mkdir()
    monkeypatch.setattr(central, "TEST_OUTPUT_ROOT", test_output)
    monkeypatch.setattr(central, "ALLOWED_SOURCE_ROOTS", {source_root})
    return db, source_root, test_output


def test_schema_audit_and_dry_run_are_database_driven(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "dry"
    result = central.run_pipeline(args_for(db, output, "dry-run"))
    assert result["status"] == "PASS"
    assert result["source_input_count"] == 3
    assert result["visual_input_count"] == 3
    assert result["commit_status"] == "NOT_COMMITTED"
    assert result["workers_1_vs_20_deterministic"] is True
    assert (output / "reports" / "schema_audit.json").is_file()
    assert not central.table_exists(central.connect_readonly(db), "source_asset_identity")


def test_source_exact_duplicate_requires_full_hash_and_stable_canonical(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "source"
    central.run_pipeline(args_for(db, output, "dry-run"))
    rows = [json.loads(line) for line in (output / "manifests" / "source_asset_identity_manifest.jsonl").read_text().splitlines()]
    duplicate_rows = [row for row in rows if row["duplicate_group_id"]]
    assert len(duplicate_rows) == 2
    assert all(row["full_content_hash"] for row in duplicate_rows)
    canonical = next(row for row in duplicate_rows if row["identity_status"] == "canonical")
    duplicate = next(row for row in duplicate_rows if row["identity_status"] == "exact_duplicate")
    assert canonical["relative_path"] == "a.jpg"
    assert duplicate["eligible_for_heavy_models"] is False


def test_peer_full_hash_failure_holds_entire_probable_duplicate_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    rows = []
    for index, path in enumerate((first, second), 1):
        stat = path.stat()
        rows.append({
            "source_file_id": f"sf_{index}", "source_content_id": f"sc_{index}", "absolute_path": str(path),
            "relative_path": path.name, "media_kind": "image", "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
        })
    real_hash = central.full_hash_file

    def one_failure(row):
        return ("", "fixture failure") if row["source_file_id"] == "sf_2" else real_hash(row)

    monkeypatch.setattr(central, "full_hash_file", one_failure)
    identities, _, _ = central.compute_source_identity(rows, run_id="r", max_workers=2, quick_bytes=4)
    assert all(row["eligible_for_heavy_models"] is False for row in identities)
    assert {row["identity_status"] for row in identities} == {"blocked", "failed"}


def test_visual_dedup_uses_existing_vectors_and_blocks_duplicate_heavy_input(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "visual"
    central.run_pipeline(args_for(db, output, "commit"))
    con = central.connect_readonly(db)
    try:
        rows = [dict(row) for row in con.execute("SELECT * FROM visual_identity ORDER BY visual_unit_id")]
        assert [row["identity_status"] for row in rows].count("near_duplicate") == 1
        duplicate_id = next(row["visual_unit_id"] for row in rows if row["identity_status"] == "near_duplicate")
        with pytest.raises(RuntimeError, match="DUPLICATE_VISUAL_CANDIDATE_ENTERED_HEAVY_STAGE"):
            central.assert_canonical_visual_for_heavy(con, [duplicate_id])
        source_duplicate = next(row[0] for row in con.execute("SELECT source_content_id FROM source_asset_identity WHERE identity_status='exact_duplicate'"))
        with pytest.raises(RuntimeError, match="NON_CANONICAL_SOURCE_ENTERED_HEAVY_STAGE"):
            central.assert_canonical_source_for_heavy(con, [source_duplicate])
    finally:
        con.close()


def test_commit_readback_reverse_mapping_and_manifest_consistency(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "commit"
    result = central.run_pipeline(args_for(db, output, "commit"))
    assert result["commit_status"] == "COMMITTED"
    assert result["manifest_db_consistency"] is True
    assert result["integrity_check"] == ["ok"]
    assert result["foreign_key_check"] == []
    con = central.connect_readonly(db)
    try:
        source_canonical = con.execute("SELECT canonical_source_content_id FROM source_asset_identity WHERE identity_status='canonical'").fetchone()[0]
        assert len(central.expand_canonical_source_to_originals(con, source_canonical)) == 2
        visual_canonical = con.execute("SELECT canonical_visual_unit_id FROM visual_identity WHERE identity_status='canonical'").fetchone()[0]
        assert len(central.expand_visual_candidate_to_originals(con, visual_canonical)) == 2
    finally:
        con.close()


def test_rerun_is_idempotent(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "idempotent"
    first = central.run_pipeline(args_for(db, output, "commit"))
    second = central.run_pipeline(args_for(db, output, "commit"))
    assert first["source_identity_row_count"] == second["source_identity_row_count"]
    assert second["rerun_idempotent"] is True
    con = central.connect_readonly(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM dedup_runs WHERE run_id='fixture_run'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM source_asset_identity").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM visual_identity").fetchone()[0] == 3
    finally:
        con.close()


def test_transaction_failure_rolls_back_schema_and_rows(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "rollback"
    with pytest.raises(RuntimeError, match="INJECTED_FAILURE_BEFORE_COMMIT"):
        central.run_pipeline(args_for(db, output, "commit", inject_failure_before_commit=True))
    con = central.connect_readonly(db)
    try:
        assert not central.table_exists(con, "source_asset_identity")
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    assert (output / "database_backup" / db.name).is_file()


def test_blocked_decoder_is_conservative_passthrough(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "blocked"
    result = central.run_pipeline(args_for(db, output, "dry-run", decode_backend="blocked"))
    assert result["policy_status"] == "REVIEW"
    rows = [json.loads(line) for line in (output / "manifests" / "frame_visual_identity_manifest.jsonl").read_text().splitlines()]
    assert all(row["identity_status"] == "blocked_decoder" for row in rows)
    assert all(row["eligible_for_heavy_models"] is True for row in rows)


def test_html_uses_relative_assets_and_contains_database_audit(fixture_env):
    db, _, test_output = fixture_env
    output = test_output / "html"
    result = central.run_pipeline(args_for(db, output, "dry-run"))
    page = Path(result["html_path"]).read_text(encoding="utf-8")
    assert "src=\"assets/" in page
    assert "file://" not in page
    assert "../" not in page
    assert "中心数据库" in page


def test_source_and_derived_inputs_are_not_modified(fixture_env):
    db, source_root, test_output = fixture_env
    source_stats = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in source_root.rglob("*") if path.is_file()}
    con = central.connect_readonly(db)
    derived_paths = [Path(row[0]) for row in con.execute("SELECT derived_path FROM derived_assets")]
    con.close()
    derived_stats = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in derived_paths}
    central.run_pipeline(args_for(db, test_output / "readonly", "dry-run"))
    assert source_stats == {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in source_stats}
    assert derived_stats == {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in derived_stats}


def test_script_has_no_model_or_network_execution_path():
    text = SCRIPT_PATH.read_text(encoding="utf-8")
    forbidden = ["mlx_vlm", "ultralytics", "ffmpeg", "whisper", "requests.get", "urllib.request", "pip install"]
    assert not any(token in text for token in forbidden)
