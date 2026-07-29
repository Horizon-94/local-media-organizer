#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stop03-5B2: patch evidence staging DB with structured YOLOE labels.
Reads existing Stop03-5B staging sqlite and Stop03-1 join manifest, then writes a copied sqlite
with yolo_visual_unit_label and yolo_visual_unit_label_summary tables.
No model run, no original media access, no network.
"""
from __future__ import annotations
import argparse, csv, json, os, re, shutil, sqlite3, sys, time
from pathlib import Path
from collections import Counter, defaultdict

LABEL_SPLIT_RE = re.compile(r"[|,;，；、\s]+")

def now_stamp():
    return time.strftime("%Y%m%d_%H%M%S")

def ensure_dirs(out: Path):
    for p in [out/"database", out/"manifests", out/"reports"]:
        p.mkdir(parents=True, exist_ok=True)

def read_csv(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))

def parse_labels(raw: str):
    if raw is None:
        return []
    s = str(raw).strip()
    if not s:
        return []
    # Try JSON list/dict first
    try:
        obj = json.loads(s)
        vals = []
        if isinstance(obj, list):
            for x in obj:
                if isinstance(x, dict):
                    vals.append(str(x.get("label") or x.get("class") or x.get("name") or ""))
                else:
                    vals.append(str(x))
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (int, float)):
                    vals.append(str(k))
                elif isinstance(v, str):
                    vals.append(v)
        labels = vals
    except Exception:
        labels = LABEL_SPLIT_RE.split(s)
    out = []
    for lab in labels:
        lab = str(lab).strip().strip("'\"[]{}()")
        if not lab:
            continue
        # remove confidence/count fragments such as person:0.83 or car=2
        lab = re.split(r"[:=]", lab, maxsplit=1)[0].strip()
        lab = lab.lower()
        if lab in {"none", "null", "nan", "[]"}:
            continue
        if len(lab) > 80:
            continue
        out.append(lab)
    # stable unique
    seen = set(); uniq = []
    for x in out:
        if x not in seen:
            seen.add(x); uniq.append(x)
    return uniq

def copy_db(src: Path, dst: Path):
    if not src.exists():
        raise FileNotFoundError(src)
    shutil.copy2(src, dst)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staging-db", required=True)
    ap.add_argument("--stop03-1-join", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--expect-visual-units", type=int, default=1628)
    args = ap.parse_args()

    staging_db = Path(args.staging_db)
    join_csv = Path(args.stop03_1_join)
    out = Path(args.out)
    ensure_dirs(out)
    db_out = out/"database"/"evidence_staging_yolo_patch.sqlite"
    copy_db(staging_db, db_out)

    rows = read_csv(join_csv)
    conn = sqlite3.connect(db_out)
    cur = conn.cursor()
    cur.executescript("""
    DROP TABLE IF EXISTS yolo_visual_unit_label;
    DROP TABLE IF EXISTS yolo_visual_unit_label_summary;
    CREATE TABLE yolo_visual_unit_label (
        visual_unit_id TEXT NOT NULL,
        label TEXT NOT NULL,
        detection_count INTEGER,
        raw_labels TEXT,
        source_manifest TEXT,
        created_at TEXT,
        PRIMARY KEY (visual_unit_id, label)
    );
    CREATE TABLE yolo_visual_unit_label_summary (
        visual_unit_id TEXT PRIMARY KEY,
        labels_pipe TEXT NOT NULL,
        label_count INTEGER NOT NULL,
        detection_count INTEGER,
        raw_labels TEXT,
        source_manifest TEXT,
        created_at TEXT
    );
    """)

    inserted = 0
    summary_rows = []
    label_counter = Counter()
    raw_nonempty = 0
    det_positive = 0
    for r in rows:
        vu = r.get("visual_unit_id") or r.get("id") or ""
        raw = r.get("yoloe_detected_labels") or r.get("yolo_detected_labels") or r.get("detected_labels") or ""
        det = r.get("yoloe_detection_count") or r.get("detection_count") or ""
        try:
            det_i = int(float(det)) if str(det).strip() else 0
        except Exception:
            det_i = 0
        if det_i > 0:
            det_positive += 1
        labels = parse_labels(raw)
        if raw.strip():
            raw_nonempty += 1
        if not vu or not labels:
            continue
        labels_pipe = "|".join(labels)
        summary_rows.append({
            "visual_unit_id": vu,
            "labels_pipe": labels_pipe,
            "label_count": len(labels),
            "detection_count": det_i,
            "raw_labels": raw,
            "source_manifest": str(join_csv),
        })
        cur.execute("INSERT OR REPLACE INTO yolo_visual_unit_label_summary VALUES (?,?,?,?,?,?,?)",
                    (vu, labels_pipe, len(labels), det_i, raw, str(join_csv), now_stamp()))
        for lab in labels:
            cur.execute("INSERT OR REPLACE INTO yolo_visual_unit_label VALUES (?,?,?,?,?,?)",
                        (vu, lab, det_i, raw, str(join_csv), now_stamp()))
            inserted += 1
            label_counter[lab] += 1
    conn.commit()

    # write manifests
    label_manifest = out/"manifests"/"yolo_visual_unit_label_manifest.csv"
    with label_manifest.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["visual_unit_id","labels_pipe","label_count","detection_count","raw_labels","source_manifest"])
        w.writeheader(); w.writerows(summary_rows)
    top_labels = out/"reports"/"yolo_label_distribution.csv"
    with top_labels.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["label","visual_unit_count"])
        for lab, n in label_counter.most_common():
            w.writerow([lab, n])

    counts = {
        "validation_status": "PASS" if summary_rows else "FAIL",
        "input_join_rows": len(rows),
        "raw_label_nonempty_rows": raw_nonempty,
        "detection_count_positive_rows": det_positive,
        "yolo_label_visual_units": len(summary_rows),
        "yolo_label_rows": inserted,
        "top_labels": dict(label_counter.most_common(20)),
        "sqlite": str(db_out),
        "label_manifest_csv": str(label_manifest),
    }
    if args.expect_visual_units and len(rows) != args.expect_visual_units:
        counts.setdefault("warnings", []).append(f"join row count {len(rows)} != expected {args.expect_visual_units}")
    summary_json = out/"reports"/"stop03_5b2_yolo_label_staging_patch_summary.json"
    summary_md = out/"reports"/"stop03_5b2_yolo_label_staging_patch_summary.md"
    summary_json.write_text(json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Stop03-5B2 YOLO Label Staging Patch", "", f"- validation_status: `{counts['validation_status']}`", "- mode: `read_existing_join_and_staging_db_only_no_model_rerun`", "- source_safety: `read_only_no_original_media_write`", "- network: `not_required_not_used`", "", "## Counts"]
    for k in ["input_join_rows","raw_label_nonempty_rows","detection_count_positive_rows","yolo_label_visual_units","yolo_label_rows"]:
        md.append(f"- {k}: `{counts[k]}`")
    md += ["", "## Top labels", "```json", json.dumps(counts["top_labels"], ensure_ascii=False, indent=2), "```", "", "## Outputs", f"- sqlite: `{db_out}`", f"- label_manifest_csv: `{label_manifest}`", f"- top_labels_csv: `{top_labels}`"]
    summary_md.write_text("\n".join(md), encoding="utf-8")
    print("== Stop03-5B2 YOLO label staging patch finished ==")
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0 if counts["validation_status"] == "PASS" else 2

if __name__ == "__main__":
    raise SystemExit(main())
