#!/usr/bin/env python3
import argparse, csv, hashlib, json, os, statistics, time, traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Event, Thread

SCRIPT_VERSION = "stop03_1b_openclip_visual_embedding_probe_v1_20260708"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

MODEL = None
PREPROCESS = None
DEVICE = None
MODEL_NAME = None

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk_size)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def stable_id(prefix, *parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return prefix + "_" + h.hexdigest()[:24]

def first_value(row, keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return ""

def read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows

def resolve_visual_file(row, preferred_keys):
    for k in preferred_keys:
        v = row.get(k)
        if v:
            p = Path(str(v))
            if p.exists() and p.suffix.lower() in IMAGE_EXTS:
                return str(p)

    for k, v in row.items():
        if not v:
            continue
        lk = str(k).lower()
        if not any(x in lk for x in ["visual", "preview", "frame", "jpg", "file", "path"]):
            continue
        p = Path(str(v))
        if p.exists() and p.suffix.lower() in IMAGE_EXTS:
            return str(p)
    return ""

def build_master(video_manifest, image_manifest, out_dir):
    out_dir = Path(out_dir)
    manifest_dir = out_dir / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    rows, problems, seen = [], [], set()

    with open(video_manifest, "r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            vf = resolve_visual_file(r, [
                "visual_file",
                "frame_file",
                "frame_path",
                "frame_jpg_path",
                "output_frame_path",
                "jpg_path",
            ])
            if not vf:
                problems.append({"source": "video_manifest", "reason": "missing_visual_file", "row": r})
                continue

            vu = first_value(r, ["visual_unit_id", "frame_id"]) or stable_id("vu_video_frame", vf)
            if vu in seen:
                continue
            seen.add(vu)

            rows.append({
                "visual_unit_id": vu,
                "visual_unit_type": "video_frame",
                "visual_file": vf,
                "producer_step": first_value(r, ["producer_step"]) or "stop02_1_video_frame",
                "parent_source_file_id": first_value(r, ["parent_source_file_id"]),
                "parent_source_content_id": first_value(r, ["parent_source_content_id"]),
                "parent_source_path_at_processing_time": first_value(r, [
                    "parent_source_path_at_processing_time",
                    "source_video_path",
                    "source_path",
                ]),
                "source_relative_path": first_value(r, [
                    "source_relative_path",
                    "source_video_relative_path",
                ]),
                "time_position_ms": first_value(r, [
                    "time_position_ms",
                    "estimated_time_ms",
                    "frame_time_ms",
                ]),
                "preview_role": first_value(r, ["preview_role"]) or "video_frame",
                "source_manifest": str(video_manifest),
            })

    for r in read_jsonl(image_manifest):
        vf = resolve_visual_file(r, [
            "visual_file",
            "preview_file",
            "preview_path",
            "image_preview_file",
            "output_file",
        ])
        if not vf:
            problems.append({"source": "image_manifest", "reason": "missing_visual_file", "row": r})
            continue

        vu = first_value(r, ["visual_unit_id", "preview_artifact_id"]) or stable_id("vu_image_preview", vf)
        if vu in seen:
            continue
        seen.add(vu)

        rows.append({
            "visual_unit_id": vu,
            "visual_unit_type": first_value(r, ["visual_unit_type"]) or "image_preview",
            "visual_file": vf,
            "producer_step": first_value(r, ["producer_step"]) or "stop02_2_image_preview",
            "parent_source_file_id": first_value(r, ["parent_source_file_id"]),
            "parent_source_content_id": first_value(r, ["parent_source_content_id"]),
            "parent_source_path_at_processing_time": first_value(r, [
                "parent_source_path_at_processing_time",
                "source_path",
            ]),
            "source_relative_path": first_value(r, ["source_relative_path"]),
            "time_position_ms": first_value(r, ["time_position_ms"]),
            "preview_role": first_value(r, ["preview_role"]) or "image_preview",
            "source_manifest": str(image_manifest),
        })

    master_jsonl = manifest_dir / "visual_unit_master_manifest.jsonl"
    master_csv = manifest_dir / "visual_unit_master_manifest.csv"
    problems_jsonl = manifest_dir / "visual_unit_master_manifest_problems.jsonl"

    with master_jsonl.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    fields = [
        "visual_unit_id",
        "visual_unit_type",
        "visual_file",
        "producer_step",
        "parent_source_file_id",
        "parent_source_content_id",
        "parent_source_path_at_processing_time",
        "source_relative_path",
        "time_position_ms",
        "preview_role",
        "source_manifest",
    ]
    with master_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    with problems_jsonl.open("w", encoding="utf-8") as f:
        for r in problems:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return rows, master_jsonl, master_csv, problems_jsonl

def monitor_resources(stop_event, out_csv, interval):
    import psutil

    fields = [
        "timestamp",
        "elapsed_seconds",
        "process_cpu_cores_estimated",
        "process_rss_mb_sum",
        "child_process_count",
        "system_cpu_percent",
        "system_memory_percent",
        "swap_used_mb",
    ]

    start = time.perf_counter()
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)

    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        root = psutil.Process(os.getpid())
        for p in [root] + root.children(recursive=True):
            try:
                p.cpu_percent(interval=None)
            except Exception:
                pass

        while not stop_event.is_set():
            time.sleep(interval)

            cpu_sum = 0.0
            rss_sum = 0.0
            child_count = 0

            try:
                root = psutil.Process(os.getpid())
                procs = [root] + root.children(recursive=True)
                child_count = max(0, len(procs) - 1)

                for p in procs:
                    try:
                        cpu_sum += p.cpu_percent(interval=None)
                        rss_sum += p.memory_info().rss / 1024 / 1024
                    except Exception:
                        pass

                vm = psutil.virtual_memory()
                sm = psutil.swap_memory()
                sys_cpu = psutil.cpu_percent(interval=None)
                sys_mem = vm.percent
                swap_mb = sm.used / 1024 / 1024
            except Exception:
                sys_cpu = sys_mem = swap_mb = ""

            w.writerow({
                "timestamp": now_iso(),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "process_cpu_cores_estimated": round(cpu_sum / 100.0, 3),
                "process_rss_mb_sum": round(rss_sum, 3),
                "child_process_count": child_count,
                "system_cpu_percent": sys_cpu,
                "system_memory_percent": sys_mem,
                "swap_used_mb": round(swap_mb, 3) if swap_mb != "" else "",
            })
            f.flush()

def init_worker(model_path, model_name, device):
    global MODEL, PREPROCESS, DEVICE, MODEL_NAME

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    import torch
    import open_clip

    if device == "auto":
        DEVICE = "mps" if hasattr(torch.backends, "mps") and torch.backends.mps.is_available() else "cpu"
    else:
        DEVICE = device

    MODEL_NAME = model_name

    try:
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name,
            pretrained=str(model_path),
            device=DEVICE,
        )
    except Exception:
        from safetensors.torch import load_file
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name,
            pretrained=None,
            device=DEVICE,
        )
        state = load_file(str(model_path), device="cpu")
        model.load_state_dict(state, strict=False)
        model = model.to(DEVICE)

    model.eval()
    MODEL = model
    PREPROCESS = preprocess

def infer_one(row):
    global MODEL, PREPROCESS, DEVICE, MODEL_NAME

    start = time.perf_counter()
    out = dict(row)
    out.update({
        "script_version": SCRIPT_VERSION,
        "branch": "openclip_visual_embedding_only",
        "worker_pid": os.getpid(),
        "device": DEVICE,
        "model_name": MODEL_NAME,
        "status": "unknown",
        "start_time": now_iso(),
        "end_time": "",
        "elapsed_ms": "",
        "visual_file_sha256": "",
        "embedding_dim": "",
        "embedding_norm": "",
        "embedding_vector_sha256": "",
        "embedding_json": "[]",
        "error_message": "",
    })

    try:
        import torch
        from PIL import Image

        visual_file = row["visual_file"]
        visual_sha = sha256_file(visual_file)

        img = Image.open(visual_file).convert("RGB")
        x = PREPROCESS(img).unsqueeze(0).to(DEVICE)

        with torch.no_grad():
            y = MODEL.encode_image(x)
            y = y.float()
            y = y / y.norm(dim=-1, keepdim=True)

        vec = y.detach().cpu().reshape(-1).tolist()
        vec_round = [round(float(v), 7) for v in vec]
        vec_json = json.dumps(vec_round, ensure_ascii=False)
        vec_sha = hashlib.sha256(vec_json.encode("utf-8")).hexdigest()

        out.update({
            "status": "success",
            "end_time": now_iso(),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
            "visual_file_sha256": visual_sha,
            "embedding_dim": len(vec_round),
            "embedding_norm": round(sum(v * v for v in vec_round) ** 0.5, 6),
            "embedding_vector_sha256": vec_sha,
            "embedding_json": vec_json,
        })
        return out

    except Exception as e:
        out.update({
            "status": "failed",
            "end_time": now_iso(),
            "elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
            "error_message": str(e) + "\n" + traceback.format_exc(limit=5),
        })
        return out

def pct(vals, q):
    if not vals:
        return None
    vals = sorted(vals)
    return vals[int(round((len(vals) - 1) * q))]

def summarize_resource(resource_csv):
    max_cpu = max_rss = max_swap = 0.0
    if not Path(resource_csv).exists():
        return max_cpu, max_rss, max_swap

    with open(resource_csv, "r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            try:
                max_cpu = max(max_cpu, float(r.get("process_cpu_cores_estimated") or 0))
            except Exception:
                pass
            try:
                max_rss = max(max_rss, float(r.get("process_rss_mb_sum") or 0))
            except Exception:
                pass
            try:
                max_swap = max(max_swap, float(r.get("swap_used_mb") or 0))
            except Exception:
                pass

    return round(max_cpu, 3), round(max_rss, 3), round(max_swap, 3)

def write_reports(out_dir, summary):
    out_dir = Path(out_dir)
    reports = out_dir / "reports"
    final = out_dir / "final_report"
    reports.mkdir(parents=True, exist_ok=True)
    final.mkdir(parents=True, exist_ok=True)

    (reports / "stop03_1b_visual_embedding_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (final / "stop03_1b_visual_embedding_final_report_latest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    keys = [
        "script_version",
        "run_invocation_id",
        "model_path",
        "model_name",
        "device",
        "workers",
        "input_visual_units",
        "processed_this_run",
        "success_count",
        "failed_count",
        "wall_seconds",
        "sum_task_seconds",
        "avg_task_ms",
        "p50_task_ms",
        "p90_task_ms",
        "max_task_ms",
        "wall_avg_ms_per_image",
        "throughput_images_per_second",
        "parallel_efficiency",
        "embedding_dim",
        "max_process_cpu_cores_estimated",
        "max_process_rss_mb_sum",
        "max_swap_used_mb",
    ]

    lines = ["# Stop03-1B OpenCLIP Visual Embedding Probe", ""]
    for k in keys:
        lines.append(f"- {k}: {summary.get(k)}")

    lines.append("")
    lines.append("## Outputs")
    for k, v in summary.get("outputs", {}).items():
        lines.append(f"- {k}: `{v}`")

    md = "\n".join(lines)
    (reports / "stop03_1b_visual_embedding_summary.md").write_text(md, encoding="utf-8")
    (final / "stop03_1b_visual_embedding_final_report_latest.md").write_text(md, encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video-manifest", required=True)
    ap.add_argument("--image-manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--model-name", default="ViT-B-32")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--device", default="auto", choices=["auto", "mps", "cpu"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--telemetry-interval", type=float, default=2.0)
    args = ap.parse_args()

    out_dir = Path(args.out)
    manifest_dir = out_dir / "manifests"
    telemetry_dir = out_dir / "telemetry"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    telemetry_dir.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    rows, master_jsonl, master_csv, problems_jsonl = build_master(
        args.video_manifest,
        args.image_manifest,
        out_dir,
    )

    if args.limit and args.limit > 0:
        rows = rows[:args.limit]

    result_jsonl = manifest_dir / "stop03_1b_visual_embedding_result_manifest.jsonl"
    result_csv = manifest_dir / "stop03_1b_visual_embedding_result_manifest.csv"
    timing_csv = telemetry_dir / "per_visual_unit_visual_embedding_timing.csv"
    resource_csv = telemetry_dir / "resource_samples.csv"

    fields = [
        "script_version",
        "branch",
        "visual_unit_id",
        "visual_unit_type",
        "visual_file",
        "producer_step",
        "parent_source_file_id",
        "parent_source_content_id",
        "parent_source_path_at_processing_time",
        "source_relative_path",
        "time_position_ms",
        "preview_role",
        "source_manifest",
        "worker_pid",
        "device",
        "model_name",
        "status",
        "start_time",
        "end_time",
        "elapsed_ms",
        "visual_file_sha256",
        "embedding_dim",
        "embedding_norm",
        "embedding_vector_sha256",
        "embedding_json",
        "error_message",
    ]

    timing_fields = [
        "visual_unit_id",
        "visual_unit_type",
        "visual_file",
        "status",
        "worker_pid",
        "device",
        "elapsed_ms",
        "embedding_dim",
        "visual_file_sha256",
        "embedding_vector_sha256",
        "start_time",
        "end_time",
        "error_message",
    ]

    print("== Stop03-1B OpenCLIP visual embedding start ==")
    print("script_version:", SCRIPT_VERSION)
    print("out:", out_dir)
    print("visual_units:", len(rows))
    print("workers:", args.workers)
    print("device:", args.device)
    print("model:", args.model)
    print("model_name:", args.model_name)

    stop_event = Event()
    monitor = Thread(
        target=monitor_resources,
        args=(stop_event, resource_csv, args.telemetry_interval),
        daemon=True,
    )
    monitor.start()

    wall_start = time.perf_counter()
    processed = []
    success = failed = 0

    with result_jsonl.open("w", encoding="utf-8") as jf, \
         result_csv.open("w", encoding="utf-8", newline="") as cf, \
         timing_csv.open("w", encoding="utf-8", newline="") as tf:

        cw = csv.DictWriter(cf, fieldnames=fields, extrasaction="ignore")
        tw = csv.DictWriter(tf, fieldnames=timing_fields, extrasaction="ignore")
        cw.writeheader()
        tw.writeheader()

        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=init_worker,
            initargs=(args.model, args.model_name, args.device),
        ) as ex:
            futures = [ex.submit(infer_one, r) for r in rows]

            for i, fut in enumerate(as_completed(futures), 1):
                r = fut.result()
                processed.append(r)

                jf.write(json.dumps(r, ensure_ascii=False) + "\n")
                jf.flush()
                cw.writerow(r)
                cf.flush()
                tw.writerow({k: r.get(k, "") for k in timing_fields})
                tf.flush()

                if r.get("status") == "success":
                    success += 1
                else:
                    failed += 1

                if i % 50 == 0 or i == len(rows):
                    print(f"[progress] {i}/{len(rows)} success={success} failed={failed}")

    wall_seconds = round(time.perf_counter() - wall_start, 3)
    stop_event.set()
    monitor.join(timeout=5)

    elapsed = [
        float(r["elapsed_ms"])
        for r in processed
        if r.get("status") == "success" and r.get("elapsed_ms") not in ("", None)
    ]

    dims = sorted(set(
        int(r["embedding_dim"])
        for r in processed
        if r.get("status") == "success" and str(r.get("embedding_dim", "")).isdigit()
    ))

    sum_task_seconds = round(sum(elapsed) / 1000, 3) if elapsed else 0
    avg_task_ms = round(statistics.mean(elapsed), 3) if elapsed else None
    p50_task_ms = round(pct(elapsed, 0.5), 3) if elapsed else None
    p90_task_ms = round(pct(elapsed, 0.9), 3) if elapsed else None
    max_task_ms = round(max(elapsed), 3) if elapsed else None

    max_cpu, max_rss, max_swap = summarize_resource(resource_csv)

    summary = {
        "script_version": SCRIPT_VERSION,
        "run_invocation_id": run_id,
        "model_path": args.model,
        "model_name": args.model_name,
        "device": args.device,
        "workers": args.workers,
        "input_visual_units": len(rows),
        "processed_this_run": len(processed),
        "success_count": success,
        "failed_count": failed,
        "wall_seconds": wall_seconds,
        "sum_task_seconds": sum_task_seconds,
        "avg_task_ms": avg_task_ms,
        "p50_task_ms": p50_task_ms,
        "p90_task_ms": p90_task_ms,
        "max_task_ms": max_task_ms,
        "wall_avg_ms_per_image": round(wall_seconds * 1000 / len(processed), 3) if processed else None,
        "throughput_images_per_second": round(len(processed) / wall_seconds, 3) if wall_seconds > 0 else None,
        "parallel_efficiency": round(sum_task_seconds / (args.workers * wall_seconds), 3) if wall_seconds > 0 and args.workers else None,
        "embedding_dim": dims[0] if len(dims) == 1 else dims,
        "max_process_cpu_cores_estimated": max_cpu,
        "max_process_rss_mb_sum": max_rss,
        "max_swap_used_mb": max_swap,
        "source_safety": "read_only_no_copy_no_move_no_delete_original_media",
        "outputs": {
            "visual_unit_master_manifest_jsonl": str(master_jsonl),
            "visual_unit_master_manifest_csv": str(master_csv),
            "visual_unit_master_manifest_problems_jsonl": str(problems_jsonl),
            "result_manifest_jsonl": str(result_jsonl),
            "result_manifest_csv": str(result_csv),
            "per_visual_unit_visual_embedding_timing_csv": str(timing_csv),
            "resource_samples_csv": str(resource_csv),
        },
    }

    write_reports(out_dir, summary)

    print("== Stop03-1B OpenCLIP visual embedding finished ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
