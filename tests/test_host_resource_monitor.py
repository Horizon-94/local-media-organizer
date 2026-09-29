import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "tools/pipeline_benchmark/host_resource_monitor.py"
SPEC = importlib.util.spec_from_file_location("host_resource_monitor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def row(pid, ppid, command, rss=10, cpu=1):
    return {"pid": pid, "ppid": ppid, "command": command, "rss_kib": rss,
            "cpu_percent": cpu, "elapsed": "00:01", "type": MODULE.classify(command)}


def test_process_tree_is_root_scoped_and_excludes_unrelated_system_processes():
    rows = [
        row(100, 1, "/usr/bin/python qwen worker.py", 100),
        row(101, 100, "/usr/bin/python -c qwen child", 200),
        row(102, 1, "/System/Library/spotlight/mdworker", 5000),
        row(103, 102, "/System/Library/embedding-service", 5000),
    ]
    scoped = MODULE.process_tree(rows, 100)
    assert {item["pid"] for item in scoped} == {100, 101}


def test_unknown_system_commands_are_not_promoted_to_workloads():
    assert MODULE.classify("/System/Library/Speech/speechrecognitiond") == "UNKNOWN"
    assert MODULE.classify("/System/Library/Spotlight/mdworker_shared") == "UNKNOWN"
    assert MODULE.classify("/usr/local/bin/ffmpeg -i input.mov") == "FFMPEG"


def test_missing_root_has_empty_workload_scope():
    assert MODULE.process_tree([row(1, 0, "qwen")], 999) == []
