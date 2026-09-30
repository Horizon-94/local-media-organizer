#!/usr/bin/env python3
"""Read-only audit of historical campaign artifacts; no inference or DB writes."""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import statistics
import sqlite3
from datetime import datetime
from pathlib import Path
from measurement_stats import p95


def stats(values):
    values = sorted(values)
    return {"avg": statistics.mean(values), "p95": p95(values), "peak": max(values)} if values else {"avg": None, "p95": None, "peak": None}


def input_fingerprint(name, result):
    """Hash stable input evidence, never the mutated DB binary or run IDs."""
    output=Path(result.get('output',''))
    if name.startswith('ffmpeg'):
        path=output/'stage/manifests/source_video_identity_step01_queue.csv'
        if not path.exists(): return None
        with path.open() as f:
            evidence=sorted((r['source_video_fingerprint_sha256'],r['sampling_contract_id']) for r in csv.DictReader(f))
    else:
        path=Path(result.get('fixture',''))
        if not path.is_file(): return None
        # Called only for completed workloads: immutable prevents SQLite from
        # attempting to create a WAL/SHM file during a read-only audit.
        try:
            with sqlite3.connect(f'file:{path}?mode=ro&immutable=1',uri=True) as c:
                if name.startswith('qwen'):
                    evidence=c.execute('select candidate_id,runtime_visual_file_sha256,candidate_semantic_sha256 from stop03_2_candidate_queue_frozen_v25 order by candidate_id').fetchall()
                elif name.startswith('embedding'):
                    evidence=c.execute('select document_id,embedding_text_sha256 from stop03_5d_text_documents order by document_id').fetchall()
                elif name.startswith('whisper'):
                    evidence=c.execute('select source_file_id,content_sha256,size_bytes,mtime_ns from source_file_records order by source_file_id').fetchall()
                else:
                    evidence=c.execute('select v.visual_unit_id,d.sha256 from visual_units v left join derived_assets d on v.derived_id=d.derived_id order by v.visual_unit_id').fetchall()
        except sqlite3.OperationalError:
            # Legacy fixtures may lack stable identity columns. Never invent
            # an equivalent input fingerprint from a mutable run database.
            return None
    return hashlib.sha256(json.dumps(evidence,separators=(',',':')).encode()).hexdigest() if evidence else None


def concurrency_evidence(name, result):
    if name.startswith('whisper'):
        events=[]
        for file in (Path(result.get('output',''))/'stage/items').glob('*/benchmark-whisper-timing.jsonl'):
            for line in file.read_text().splitlines():
                r=json.loads(line); events.extend([(r['started_epoch'],1),(r['finished_epoch'],-1)])
        active=peak=0
        for _,delta in sorted(events): active+=delta; peak=max(peak,active)
        # Never promote audio preparation/VAD overlap to Whisper inference.
        return peak if events else None
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


def backend_intervals(name,result):
    root=Path(result.get('output',''))
    files=list((root/'stage/items').glob('*/benchmark-whisper-timing.jsonl')) if name=='whisper' else list((root/'stage/benchmark-task-timing').glob('worker-*.jsonl'))
    if files:
        return [(r['started_epoch'],r['finished_epoch']) for file in files for line in file.read_text().splitlines() for r in [json.loads(line)]]
    table={'qwen':'stop03_3_qwenvl_run_items','ocr':'stop03_4_ocr_attempts','embedding':'stop03_5d_text_vectors'}.get(name)
    path=result.get('fixture')
    if not table or not path: return []
    with sqlite3.connect(f'file:{path}?mode=ro&immutable=1',uri=True) as c:
        return [(datetime.fromisoformat(a).timestamp(),datetime.fromisoformat(b).timestamp()) for a,b in c.execute(f'select started_at,finished_at from {table} where started_at is not null and finished_at is not null')]


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
    if 'constituents' not in result:
        from run_full_pipeline_calibration import formal_stage_result
        kind=result.get('kind') or ('person-reid' if name.startswith('person-reid') else name.split('-')[0])
        semantic_ok,semantic_reason=formal_stage_result(kind,Path(result.get('output','')))
        if not semantic_ok:
            row.update(validity='INVALID',reason=semantic_reason,resource_verdict='HARNESS_INVALID')
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
    def measured(key): return [float(r[key]) for r in active if r.get(key) not in (None,'')]
    for prefix,key in (('compressed','memory_compressed_bytes'),('swap','swap_used_bytes')):
        values=measured(key)
        row.update({prefix+'_baseline':values[0] if values else None,prefix+'_end':values[-1] if values else None,prefix+'_peak':max(values) if values else None})
    row['active_window_swap_delta']=row['swap_end']-row['swap_baseline'] if row['swap_end'] is not None and row['swap_baseline'] is not None else None
    used=[float(r['physical_memory_bytes'])-float(r['memory_available_approx_bytes']) for r in active if r.get('physical_memory_bytes') and r.get('memory_available_approx_bytes')]
    row['system_memory_including_cache_baseline']=used[0] if used else None
    row['system_memory_including_cache_peak']=max(used) if used else None
    row['system_memory_including_cache_end']=used[-1] if used else None
    disks=measured('disk_cumulative_mib')
    row['workload_disk_combined_delta_mib']=disks[-1]-disks[0] if len(disks)>1 else None
    row['post_exit_observations']=m.get('post_exit_observations')
    output = Path(result.get("output", ""))
    reports = list(output.glob("stage/reports/*summary*.json"))
    count = None
    for p in reports:
        d = json.loads(p.read_text())
        for key in ("candidate_count", "processed_this_run", "document_count"):
            if key in d: count = d[key]
        if "counts" in d and "processed_units" in d["counts"]: count = d["counts"]["processed_units"]
        worker_peaks=[w['peak_rss_bytes'] for w in d.get('worker_reports',[]) if w.get('peak_rss_bytes')]
        if worker_peaks:
            row['formal_worker_peak_rss_bytes']=worker_peaks
    if name.startswith("qwen"):
        p=output/'qwen-stage/reports/final_report.json'
        if p.exists():
            qwen=json.loads(p.read_text()); count=qwen.get('counts',{}).get('success')
            row['mlx_per_worker_snapshots']=[w.get('mlx_memory_bytes') for w in qwen.get('worker_reports',[]) if w.get('mlx_memory_bytes')]
    if name.startswith('openclip'):
        p=output/'stage/reports/stop03_1b_openclip_db_safe_summary.json'
        if p.exists(): count=json.loads(p.read_text()).get('success_count')
    if name.startswith("whisper"):
        p=output / "stage/audio_enrichment_summary.json"
        if p.exists():
            audio=json.loads(p.read_text()); count=audio.get('source_video_count')
            row['speech_inference_inputs']=audio.get('counts',{}).get('success',0)
            row['non_speech_inputs']=audio.get('counts',{}).get('no_speech',0)
        row['mlx_per_worker_snapshots']=[json.loads(line).get('mlx_memory_bytes_at_transcribe_return') for file in (output/'stage/items').glob('*/benchmark-whisper-timing.jsonl') for line in file.read_text().splitlines()]
    if name.startswith("person"):
        p=output / "stage/run_summary.json"
        if p.exists(): count=json.loads(p.read_text()).get("visual_unit_count")
    row["processed_units"] = count
    row["throughput"] = count/wall if count and wall > 0 else None
    if row.get('formal_worker_peak_rss_bytes'):
        row['rss_peak_observed_lower_bound']=max([row.get('rss_peak') or 0]+row['formal_worker_peak_rss_bytes'])
        row['sampled_peak_below_formal_worker_peak']=max(row['formal_worker_peak_rss_bytes'])>row.get('rss_peak',0)
        row['peak_scope_note']='Per-worker lifetime ru_maxrss is not a synchronized aggregate peak. A sampled tree peak below any worker lifetime peak is an under-sampled bound; never sum worker lifetime peaks as an observed tree peak.'
    if 'constituents' not in result:
        row['input_evidence_fingerprint']=input_fingerprint(name,result)
    if name.startswith('embedding') and result.get('fixture'):
        with sqlite3.connect(f"file:{result['fixture']}?mode=ro&immutable=1",uri=True) as c:
            vectors=c.execute("select model_dimension,vector_byte_length,length(vector_blob) from stop03_5d_text_vectors where status='success'").fetchall()
        row['embedding_rows_committed']=len(vectors)
        row['embedding_dimensions']=sorted({v[0] for v in vectors})
        if not vectors or len(vectors)!=count or any(v!=(1024,4096,4096) for v in vectors):
            row.update(validity='INVALID',reason='embedding_vector_rows_or_dimensions_invalid',resource_verdict='HARNESS_INVALID')
            return row
    if 'constituents' in result:
        kinds=name.split('+')
        constituent_rows=[inspect_result(kinds[i] if i<len(kinds) else 'constituent',c) for i,c in enumerate(result['constituents'])]
        # Constituent result IDs are not guaranteed to be stored, so expose
        # per-constituent counts instead of fabricating a homogeneous rate.
        row['constituent_processed_units']=[r.get('processed_units') for r in constituent_rows]
        row['requested_concurrency']='+'.join(str(c.get('workers',1)) for c in result['constituents'])
        row['constituent_workload_metrics']=[{k:r.get(k) for k in ('case','validity','processed_units','workload_wall_time','throughput','effective_concurrency','input_evidence_fingerprint')} for r in constituent_rows]
        row['aggregate_units_are_heterogeneous']=True
        row['overlap_seconds']=result.get('all_constituents_overlap_seconds')
        if any(c['validity']=='INVALID' for c in constituent_rows):
            row.update(validity='INVALID',reason='constituent_semantic_or_measurement_contract_invalid',resource_verdict='HARNESS_INVALID')
        elif row['overlap_seconds'] and all(c.get('status')=='PASS' for c in result['constituents']):
            row.update(validity='VALID',reason='synchronized_aggregate_with_overlapping_formal_workloads',effective_concurrency=len(result['constituents']))
        if 'whisper' in kinds:
            wi=backend_intervals('whisper',result['constituents'][kinds.index('whisper')])
            overlaps={kind:sum(max(0,min(b,d)-max(a,c)) for a,b in wi for c,d in backend_intervals(kind,child)) for kind,child in zip(kinds,result['constituents']) if kind not in {'whisper','ffmpeg'}}
            row['whisper_backend_overlap_seconds']=overlaps
            if overlaps and any(v<=0 for v in overlaps.values()) and row['validity']=='VALID':
                row.update(validity='SUSPICIOUS',reason='process_overlap_without_whisper_visual_backend_overlap',resource_verdict='NOT_PROVEN_MODEL_COEXISTENCE')
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
        if limit=='1' and result.get('workers')==1 and count==1:
            row.update(validity='VALID',reason='single_video_resource_only_not_ten_video_scaling_baseline',effective_concurrency=1)
        telemetry=output/'stage/telemetry/step02_video_frame_resource_samples_latest.csv'
        per_video=output/'stage/telemetry/step02_video_frame_per_video_timing_latest.csv'
        if per_video.exists():
            with per_video.open() as f: video_rows=list(csv.DictReader(f))
            durations=[float(r['task_elapsed_seconds']) for r in video_rows if r.get('task_elapsed_seconds')]
            frames=[int(r['valid_jpg1280_count']) for r in video_rows if r.get('valid_jpg1280_count')]
            row['video_task_sum_seconds']=sum(durations) if durations else None
            row['longest_video_task_seconds']=max(durations) if durations else None
            row['longest_task_fraction_of_wall']=max(durations)/wall if durations and wall>0 else None
            row['largest_video_fraction_of_frames']=max(frames)/sum(frames) if frames and sum(frames) else None
            row['ffmpeg_scaling_scope']='Fixed ten-video batch; skewed video lengths can cap batch speedup. Not a hardware-wide worker ceiling.'
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
    if name.startswith('whisper') and int(result.get('workers',1))>1 and row.get('speech_inference_inputs',0)<int(result['workers']):
        row.update(validity='INVALID',reason='audio_jobs_overlap_but_too_few_whisper_inference_inputs')
    if row['validity']=='SUSPICIOUS' and not name.startswith('ffmpeg'):
        effective=concurrency_evidence(name,result)
        row['effective_concurrency']=effective
        if effective and effective >= int(result.get('workers',1)):
            row.update(validity='VALID',reason='short_baseline_with_measured_formal_work_overlap')
    return row


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root",type=Path,required=True); ap.add_argument("--out",type=Path,required=True); ap.add_argument('--supplement',type=Path,action='append',default=[]); a=ap.parse_args()
    data=json.loads((a.root/"calibration-results.json").read_text())
    rows=[inspect_result(k,v) for k,v in data["results"].items()]
    for supplement in a.supplement:
        revised=json.loads((supplement/'calibration-results.json').read_text())['results']
        for name,result in revised.items():
            original=next((r for r in rows if r['case']==name),None)
            replacement=inspect_result(name,result)
            def inputs(r):
                return [(c.get('case'),c.get('processed_units'),c.get('input_evidence_fingerprint')) for c in r.get('constituent_workload_metrics',[])] or [(r.get('processed_units'),r.get('input_evidence_fingerprint'))]
            if original is None or replacement['validity']!='VALID' or inputs(original)!=inputs(replacement):
                raise RuntimeError(f'supplement_not_valid_or_input_drift:{name}')
            replacement['superseded_validity']=original['validity']
            replacement['superseded_reason']=original['reason']
            replacement['revision_source']=str(supplement)
            rows[rows.index(original)]=replacement
            data['results'][name]=result
    # Compare only validated fixed-volume homogeneous cases, never a mixture
    # of one-video and ten-video timings or heterogeneous units.
    grouped={}
    for row in rows:
        original=data['results'][row['case']]
        if row['validity']=='VALID' and row.get('throughput') and '+' not in row['case']:
            grouped.setdefault((original.get('kind'),row.get('processed_units')),[]).append(row)
    for group in grouped.values():
        group.sort(key=lambda r:r.get('requested_concurrency') or 1)
        baseline=next((r for r in group if r.get('requested_concurrency')==1),None)
        fingerprints={r.get('input_evidence_fingerprint') for r in group}
        if len(group)>1 and (None in fingerprints or len(fingerprints)!=1):
            for r in group:
                r.update(validity='INVALID',reason='homogeneous_input_fingerprints_differ_or_missing',resource_verdict='HARNESS_INVALID')
            continue
        previous=None
        for row in group:
            n=row.get('requested_concurrency') or 1
            if baseline:
                row['throughput_gain_vs_x1']=row['throughput']/baseline['throughput']
                row['scaling_efficiency']=row['throughput_gain_vs_x1']/n
            if previous:
                gain=row['throughput']/previous['throughput']-1
                row['marginal_gain_vs_previous']=gain
                if gain<=0 and row.get('resource_verdict')=='SAFE': row['resource_verdict']='SAFE_BUT_INEFFICIENT'
            previous=row
    a.out.mkdir(parents=True,exist_ok=True)
    (a.out/"validity-audit.json").write_text(json.dumps(rows,ensure_ascii=False,indent=2)+"\n")
    fields=sorted(set().union(*(r.keys() for r in rows)))
    with (a.out/"validity-audit.csv").open("w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    lines=["# CALIBRATION VALIDITY AUDIT", "", "Wall time excludes post-exit observation. P95 is empirical nearest-rank, recalculated from raw samples. RSS is process memory, not GPU VRAM. CPU 100% represents approximately one logical core; divide by 10 for machine normalization. System used/available estimates include/exclude reclaimable cache and are NOT Activity Monitor application memory. One-second sampling can miss brief peaks.","", "| case | validity | requested / effective | processed units | work sec | units/sec | RSS avg/P95/peak GiB | CPU avg/P95/peak % | compressed peak bytes | pressure free min % | swap delta bytes | active system I/O MiB | resource verdict | reason |", "|---|---|---|---:|---:|---:|---|---|---:|---:|---:|---:|---|---|"]
    for r in rows:
        fmt=lambda k,s: '/'.join(f'{r[k+"_"+t]/s:.2f}' if r.get(k+"_"+t) is not None else 'N/A' for t in ('avg','p95','peak'))
        lines.append(f"| {r['case']} | {r['validity']} | {r.get('requested_concurrency')} / {r.get('effective_concurrency')} | {r.get('processed_units','N/A')} | {r.get('workload_wall_time', 'N/A')} | {r.get('throughput','N/A')} | {fmt('rss',2**30)} | {fmt('cpu',1)} | {r.get('compressed_peak','N/A')} | {r.get('memory_pressure_minimum','N/A')} | {r.get('swap_delta','N/A')} | {r.get('workload_disk_combined_delta_mib','N/A')} | {r.get('resource_verdict','N/A')} | {r['reason']} |")
    lines += ["", "SAFE profile: NOT PROVEN. MAX_THROUGHPUT profile: NOT PROVEN. Production scheduler resource costs remain pending valid concurrency, aggregate overlap and sustained validation. Disk totals are whole-system combined I/O; separate read/write and GPU utilization are N/A."]
    (a.out/"validity-audit.md").write_text('\n'.join(lines)+'\n')
    proposal=['# Observed admission resource table (NOT a production budget)', '',
              'Only validated x1 rows below. Cold whole-workload RSS peak is an observed reservation reference, not a proven upper bound. MLX allocator snapshots are per worker, not independent VRAM; never add them to RSS. Startup-only allowance is N/A because no exact load/steady-state phase boundary was measured.', '',
              '| workload | sampled RSS P95 / peak GiB | formal max worker RSS peak GiB | CPU P95 cores | startup-only allowance | coexistence evidence |',
              '|---|---:|---:|---:|---|---|']
    mixes=[r for r in rows if '+' in r['case'] and r['validity']=='VALID']
    for r in rows:
        if r['validity']=='VALID' and r.get('requested_concurrency')==1 and '+' not in r['case']:
            evidence=', '.join(m['case'] for m in mixes if r['case'].split('-')[0] in m['case'].split('+')) or 'N/A'
            worker_peak=max(r.get('formal_worker_peak_rss_bytes') or [0])
            formal_peak=f'{worker_peak/2**30:.3f}' if worker_peak else 'N/A'
            proposal.append(f"| {r['case']} | {r['rss_p95']/2**30:.3f} / {r['rss_peak']/2**30:.3f} | {formal_peak} | {r['cpu_p95']/100:.3f} | N/A | {evidence} (short cold overlap only) |")
    proposal += ['', 'SAFE production profile: NOT PROVEN. MAX_THROUGHPUT whole-pipeline profile: NOT PROVEN.',
                 'Measured local combinations are resource coexistence evidence, not an optimal total-media schedule. Different models have heterogeneous units; do not sum units/sec or compare different work mixes as an overall pipeline speedup.',
                 'No production scheduler defaults were changed. Next validation must cover sustained mixed execution, startup/load reserve, background headroom and shared DB writer. HDD limits remain unmeasured without a real HDD fixture.']
    (a.out/'observed-admission-resources.md').write_text('\n'.join(proposal)+'\n')
    print(a.out/"validity-audit.md")
if __name__=="__main__": main()
