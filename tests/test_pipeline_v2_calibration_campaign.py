import importlib.util
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_qwen_history_scrub_and_execution_key_audit(tmp_path: Path):
    fixture = tmp_path / "fixture.sqlite"
    con = sqlite3.connect(fixture)
    con.executescript(
        """
        CREATE TABLE stop03_3_qwenvl_runs(run_id TEXT PRIMARY KEY);
        CREATE TABLE stop03_3_qwenvl_run_items(
          run_item_id TEXT PRIMARY KEY, candidate_id TEXT, execution_key TEXT, status TEXT
        );
        CREATE TABLE stop03_3_qwenvl_results(result_id TEXT PRIMARY KEY);
        INSERT INTO stop03_3_qwenvl_runs VALUES ('old-run');
        INSERT INTO stop03_3_qwenvl_run_items VALUES ('old-item','c1','old-key','success');
        INSERT INTO stop03_3_qwenvl_results VALUES ('old-result');
        """
    )
    con.commit()
    mod = load_module(ROOT / "tools/pipeline_benchmark/prepare_qwen_host_fixture.py", "prepare_qwen_fixture_test")
    scrub = mod.scrub_qwen_history(con)
    assert sum(scrub["before"].values()) == 3
    assert all(value == 0 for value in scrub["after"].values())
    assert mod.assert_no_existing_execution_keys(con, ["c1"])["existing_count"] == 0
    con.close()


def test_readiness_separates_contract_and_execution_adapter():
    mod = load_module(ROOT / "tools/pipeline_benchmark/run_full_pipeline_calibration.py", "calibration_campaign_test")
    rows = {row["id"]: row for row in mod.readiness()}
    assert rows["qwen"]["readiness"] == "CONTRACT_READY"
    assert rows["qwen"]["execution_readiness"] in {"ADAPTER_READY", "ADAPTER_IMPLEMENTED_HOST_UNVERIFIED"}
    for name in ("whisper", "yoloe", "openclip", "ocr", "embedding", "ffmpeg", "person-reid"):
        assert rows[name]["readiness"] == "CONTRACT_READY"
        assert rows[name]["execution_readiness"] in {"ADAPTER_READY", "ADAPTER_IMPLEMENTED_HOST_UNVERIFIED"}
    assert rows["silero-vad"]["readiness"] == "OPTIONAL_NOT_ACTIVE"
