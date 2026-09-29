from pathlib import Path

from apps.media_archive_image_video_ui.pipeline_v2 import (
    EventDrivenScheduler,
    PipelineStore,
    ResourceBudget,
    WorkItem,
)


def test_dependency_event_and_single_writer(tmp_path: Path):
    store = PipelineStore(tmp_path / "state.sqlite")
    scheduler = EventDrivenScheduler(
        store,
        budgets={"decode": ResourceBudget(workers=1), "label": ResourceBudget(workers=1)},
    )
    scheduler.add([
        WorkItem("decode", "a", execution_key="decode:a"),
        WorkItem("label", "a", depends_on=(("decode", "a"),), execution_key="label:a"),
    ])
    seen = []
    result = scheduler.run({
        "decode": lambda item: seen.append((item.stage_key, item.item_key)) or {"frames": 2},
        "label": lambda item: seen.append((item.stage_key, item.item_key)) or {"labels": 1},
    })
    assert seen == [("decode", "a"), ("label", "a")]
    assert [row["status"] for row in result] == ["completed", "completed"]


def test_pause_resume_and_idempotent_registration(tmp_path: Path):
    store = PipelineStore(tmp_path / "state.sqlite")
    scheduler = EventDrivenScheduler(store, {"stage": ResourceBudget(workers=1)})
    item = WorkItem("stage", "a", execution_key="stable-key")
    scheduler.add([item, item])
    assert len(store.snapshot()) == 1
    scheduler.pause()
    scheduler.resume()
    scheduler.run({"stage": lambda _: "ok"})
    assert store.status(("stage", "a")) == "completed"
