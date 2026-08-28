import copy
from pathlib import Path
import pytest

from apps.media_archive_image_video_ui_v125_candidate.editorial_session import manifest_session
from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate.core import build_board
from apps.media_archive_image_video_ui_v125_candidate.native_bridge import build_parser


def manifest():
    return {"contract_version":"editorial_manifest_v125_v1", "track":"documentary", "frame_rate":"30000/1001", "include_backups":True,
            "beats":[{"beat_id":f"beat-{i:02}","order":i,"text":f"设备阶段{i}。"} for i in range(1,4)],
            "decisions":[{"beat_id":"beat-01","candidate_id":"a","decision":"selected"},
                         {"beat_id":"beat-02","candidate_id":"b","decision":"review"}],
            "items":[{"script_id":"beat-01", "candidate_id":"a", "source_in":0,"source_out":2345,"cut_locked":True},
                     {"script_id":"beat-02", "alternatives":[{"candidate_id":"b","source_in":3456,"source_out":6000,"user_choice":"review"}]},
                     {"script_id":"beat-03", "alternatives":[]}]}


def test_preserves_incomplete_order_choices_exact_cuts_and_fps(tmp_path):
    source = manifest(); before = copy.deepcopy(source)
    result = manifest_session(source, {"candidates":[]}, tmp_path/"readonly.sqlite")
    assert result["active_beat"] == 2 and len(result["board"]["beats"]) == 3
    assert result["decisions"] == {"beat-01::a":"selected","beat-02::b":"review"}
    assert result["cut_overrides"]["beat-01::a"] == [0,2.345]
    assert result["cut_overrides"]["beat-02::b"] == [3.456,6]
    assert result["locked_cuts"] == ["beat-01::a"]
    assert result["frame_rate"] == "30000/1001" and result["include_backups"]
    assert source == before
    assert "当前素材库未找到" in result["board"]["beats"][0]["candidates"][0]["display_title"]


def test_keep_aroll_and_rejections_are_not_discarded(tmp_path):
    m = manifest(); m["items"][0].update(candidate_id="keep-a-roll::beat-01",source_in=None,source_out=None,start_ms=0,end_ms=3000)
    m["decisions"][0]["candidate_id"]="keep-a-roll::beat-01"
    m["decisions"][1]["decision"]="rejected"
    r=manifest_session(m,{"candidates":[]},tmp_path/"db")
    assert r["board"]["beats"][0]["a_roll_option"]["is_placeholder"]
    assert r["decisions"]["beat-02::b"]=="rejected"


@pytest.mark.parametrize("change", ["invalid_range","unknown_choice","missing_candidate","duplicate_beat"])
def test_invalid_import_never_silently_drops_choices(change,tmp_path):
    m=manifest()
    if change=="invalid_range": m["items"][0]["source_out"]=-1
    if change=="unknown_choice": m["decisions"][0]["decision"]="wat"
    if change=="missing_candidate": m["decisions"][0]["candidate_id"]="lost"
    if change=="duplicate_beat": m["beats"][2]["beat_id"]="beat-01"
    with pytest.raises(ValueError): manifest_session(m,{"candidates":[]},tmp_path/"db")


def test_restore_does_not_rank_or_run_models(monkeypatch,tmp_path):
    import apps.media_archive_image_video_ui_v125_candidate.editorial_session as module
    from types import SimpleNamespace
    import json
    m=manifest(); path=tmp_path/"old.json"; path.write_text(json.dumps(m))
    rows=[{"candidate_id":"a","source_content_id":"sa","source_file":"a.mov"}, {"candidate_id":"b","source_content_id":"sb","source_file":"b.mov"}]
    monkeypatch.setattr(module,"_engine",lambda:(None,SimpleNamespace(load_database_project=lambda *_a,**_kw:{"candidates":rows}),None))
    r=module.import_manifest_session(tmp_path/"db",path)
    assert len(r["decisions"])==2 and r["board"]["model_run"] is False
    args=build_parser().parse_args(["--config","cfg","editorial-import-manifest","--input",str(path)])
    assert args.input==path


def test_native_import_entry_returns_success_status(monkeypatch,tmp_path,capsys):
    import apps.media_archive_image_video_ui_v125_candidate.native_bridge as bridge
    from types import SimpleNamespace
    import json
    result=manifest_session(manifest(),{"candidates":[]},tmp_path/"db")
    monkeypatch.setattr(bridge,"load_runtime",lambda _:(None,SimpleNamespace(db_path=tmp_path/"db"),None))
    monkeypatch.setattr(bridge,"import_manifest_session",lambda *_:result)
    code=bridge.main(["--config","cfg","editorial-import-manifest","--input","old.json"])
    assert code==0
    assert json.loads(capsys.readouterr().out)["status"]=="PASS"


def test_embedded_guidance_remains_bound_to_exact_script(tmp_path):
    from v126_test_fixtures import _fixture
    from apps.media_archive_image_video_ui_v125_candidate.editorial_candidate.db_adapter import load_database_project
    db,_=_fixture(tmp_path); project=load_database_project(db)
    saved=[{"beat_id":"beat-01","text":"镜头记录了现场。","project_editorial_guidance":{"primary_shot":"4月2日现场","visual_direction":"设备细节","match_confidence":1,"guidance_status":"READY"}}]
    board=build_board(saved[0]["text"],project,"documentary",bound_guidance=saved)
    assert board["beats"][0]["editorial_guide"]["primary_shot"]=="4月2日现场"
    with pytest.raises(ValueError,match="句序"):
        build_board("换了一个新文稿。",project,"documentary",bound_guidance=saved)
