#!/usr/bin/env python3
"""Read-only audit of historical campaign artifacts; no inference or DB writes."""
from __future__ import annotations
import argparse
import csv
import json
import statistics
import sqlite3
from datetime import datetime
from pathlib import Path


def stats(values):
    values = sorted(values)
    return {"avg": statistics.mean(values), "p95": values[int((len(values)-1)*.95)], "peak": max(values)} if values else {"avg": None, "p95": None, "peak": None}


def concurrency_evidence(name, result):
    timing=Path(result.get('output',''))/'stage/benchmark-task-timing'
    events=[]
    for file in timing.glob('worker-*.jsonl'):
        for line in file.read_text().splitlines():
            record=json.loads(line)
            events.extend([(record['started_epoch'],1),(record['finished_epoch'],-1)])
    if events:
        active=peak=0
        for _,delta in sorted(events): active+=delta; peak=max(peak,active)
        return peak
    table = "stop03_4_ocr_attempts" if name.startswith("ocr") else "stop03_3_qwenvl_run_items" if name.startswith("qwen") else "stop03_5d_text_vectors" if name.startswith("embedding") else "audio_processing_items" if name.startswith("whisper") else None
    path = result.get("fixture")
    if not table or not path: return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        intervals = c.execute(f"select started_at,finished_at from {table} where started_at is not null and finished_at is not null").fetchall()
    events=[]
    for start, end in intervals:
        a,b=datetime.fromisoformat(start).timestamp(),datetime.fromisoformat(end).timestamp()
        if b>a: events.extend([(a,1),(b,-1)])
    active=peak=0
    for _, delta in sorted(events):
        active+=delta; peak=max(peak,active)
    return peak if events else None


def inspect_result(name, result):
    m = result.get("metrics", {})
    row = {"case": name, "requested_concurrency": result.get("workers"), "effective_concurrency": None,
           "validity": "SUSPICIOUS", "reason": "inference_overlap_not_proven",
           "semantic_status": result.get('status'), "resource_verdict": result.get('resource_verdict'),
           "long_term_safety": 'NOT_PROVEN'}
    if result.get("status") != "PASS":
        status=str(result.get('status'))
        row.update(validity="NOT_SUPPORTED" if "SUPPORTED" in status else "SKIPPED" if status=='SKIPPED' else "INVALID", reason=result.get("reason_code"))
        return row
    if "constituents" in result and not m.get('artifacts',{}).get('samples_csv'):
        row.update(reason="historical_composite_has_no_synchronized_aggregate_monitor")
        return row
    sample = m.get("artifacts", {}).get("samples_csv")
    if not sample or not Path(sample).is_file():
        row.update(validity="INVALID", reason="samples_missing")
        return row
    with Path(sample).open() as f:
        active = [r for r in csv.DictReader(f) if r.get("phase", "active") == "active"]
    if result.get('constituent_intervals'):
        first=min(a for a,b in result['constituent_intervals']); last=max(b for a,b in result['constituent_intervals'])
        active=[r for r in active if first <= float(r['timestamp_epoch']) <= last]
    for label, key, multiplier in (("rss", "process_rss_kib", 1024), ("cpu", "process_cpu_percent", 1)):
        for stat, value in stats([float(r[key])*multiplier for r in active if r.get(key)]).items(): row[f"{label}_{stat}"] = value
    events = {e["event"]: e["timestamp_epoch"] for e in m.get("events", [])}
    wall = events.get("PROCESS_EXIT", 0)-events.get("BENCHMARK_START", 0)
    if result.get('constituent_intervals'): wall=result.get('workload_elapsed_seconds',wall)
    row.update(workload_wall_time=wall if wall > 0 else None, monitor_total_elapsed=m.get("elapsed_seconds"),
               compressed_peak=m.get("peak_compressed_memory_bytes"), swap_delta=m.get("swap_delta_bytes"), disk_delta_mib=m.get("system_disk_delta_mib"),
               memory_pressure_minimum=min((float(r["memory_pressure_free_percent"]) for r in active if r.get("memory_pressure_free_percent")),default=None))
    row['cpu_normalized_machine_avg_percent']=row.get('cpu_avg',0)/10
    row['disk_read_bytes']=None
    row['disk_write_bytes']=None
    row['gpu_memory_process']=None
    row['measurement_note']='RSS is not unified GPU allocation. System disk is combined whole-machine I/O. Process CPU is ps sampling, not exact CPU-time integration.'
    output = Path(result.get("output", ""))
    reports = list(output.glob("stage/reports/*summary*.json"))
    count = None
    for p in reports:
        d = json.loads(p.read_text())
        for key in ("candidate_count", "processed_this_run", "document_count"):
            if key in d: count = d[key]
        if "counts" in d and "processed_units" in d["counts"]: count = d["counts"]["processed_units"]
    if name.startswith("qwen"):
        p=output/'qwen-stage/reports/final_report.json'
        if p.exists(): count=json.loads(p.read_text()).get('counts',{}).get('success')
    if name.startswith("whisper"):
        p=output / "stage/audio_enrichment_summary.json"
        if p.exists(): count=json.loads(p.read_text()).get("source_video_count")
    if name.startswith("person"):
        p=output / "stage/run_summary.json"
        if p.exists(): count=json.loads(p.read_text()).get("visual_unit_count")
    row["processed_units"] = count
    row["throughput"] = count/wall if count and wall > 0 else None
    if 'constituents' in result:
        kinds=name.split('+')
        constituent_rows=[inspect_result(kinds[i] if i<len(kinds) else 'constituent',c) for i,c in enumerate(result['constituents'])]
        # Constituent result IDs are not guaranteed to be stored, so expose
        # per-constituent counts instead of fabricating a homogeneous rate.
        row['constituent_processed_units']=[r.get('processed_units') for r in constituent_rows]
        row['aggregate_units_are_heterogeneous']=True
        row['overlap_seconds']=result.get('all_constituents_overlap_seconds')
        if row['overlap_seconds'] and all(c.get('status')=='PASS' for c in result['constituents']):
            row.update(validity='VALID',reason='synchronized_aggregate_with_overlapping_formal_workloads',effective_concurrency=len(result['constituents']))
        return row
    if name == "whisper-resident-x1": row.update(validity="INVALID", reason="fresh_process_not_resident_lifecycle")
    elif name.startswith("ffmpeg"):
        cmd=m.get("command", [])
        limit=cmd[cmd.index("--limit-new")+1] if "--limit-new" in cmd else None
        p=output/'stage/reports/video_frame_c4s_step01_queue_summary.json'
        if p.exists():
            d=json.loads(p.read_text()); count=d.get('video_report_rows_this_run'); row['processed_units']=count
            row['throughput']=count/wall if count and wall>0 else None
            row['frames_per_second']=d.get('total_valid_jpg1280_count_in_report',0)/wall if wall>0 else None
        row.update(validity="INVALID" if result.get("workers",1)>1 and limit=="1" else "SUSPICIOUS", reason="limit_new_1_not_scaling_dataset" if limit=='1' else "awaiting_verified_video_task_overlap")
        telemetry=output/'stage/telemetry/step02_video_frame_resource_samples_latest.csv'
        if limit=='10' and count==10 and telemetry.exists():
            with telemetry.open() as f: samples=list(csv.DictReader(f))
            effective=max((int(r.get('ffmpeg_pid_count') or 0) for r in samples),default=0)
            row['effective_concurrency']=effective
            if effective >= int(result.get('workers',1)):
                row.update(validity='VALID',reason='fixed_ten_videos_with_simultaneous_ffmpeg_processes')
    elif result.get("workers") == 1 and count:
        row.update(validity="VALID", reason="single_workload_resource_short_baseline", effective_concurrency=1)
    elif count and count < int(result.get("workers",1)):
        row.update(validity="INVALID", reason="fewer_inputs_than_requested_workers")
    if row['validity']=='SUSPICIOUS' and not name.startswith('ffmpeg'):
        effective=concurrency_evidence(name,result)
        row['effective_concurrency']=effective
        if effective and effective >= int(result.get('workers',1)):
            row.update(validity='VALID',reason='short_baseline_with_overlapping_committed_task_intervals')
    return row


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root",type=Path,required=True); ap.add_argument("--out",type=Path,required=True); a=ap.parse_args()
    data=json.loads((a.root/"calibration-results.json").read_text())
    rows=[inspect_result(k,v) for k,v in data["results"].items()]
    a.out.mkdir(parents=True,exist_ok=True)
    (a.out/"validity-audit.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2)+"\n")
    fields=sorted(set().union(*(r.keys() for r in rows)))
    with (a.out/"validity-audit.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    lines=["# CALIBRATION VALIDITY AUDIT", "", "Historical wall time excludes post-exit observation. RSS is process memory, not GPU VRAM. CPU 100% represents approximately one logical core; divide by 10 for machine normalization.","", "| case | validity | work sec | RSS avg/P95/peak GiB | CPU avg/P95/peak % | reason |", "|---|---|---:|---|---|---|"]
    for r in rows:
        fmt=lambda k,s: '/'.join(f'{r[k+"_"+t]/s:.2f}' if r.get(k+"_"+t) is not None else 'N/A' for t in ('avg','p95','peak'))
        lines.append(f"| {r['case']} | {r['validity']} | {r.get('workload_wall_time', 'N/A')} | {fmt('rss',2**30)} | {fmt('cpu',1)} | {r['reason']} |")
    lines += ["", "SAFE profile: NOT PROVEN. MAX_THROUGHPUT profile: NOT PROVEN. Production scheduler resource costs remain pending valid concurrency, aggregate overlap and sustained validation. Disk totals are whole-system combined I/O; separate read/write and GPU utilization are N/A."]
    (a.out/"validity-audit.md").write_text('\n'.join(lines)+'\n')
    print(a.out/"validity-audit.md")
if __name__=="__main__": main()
