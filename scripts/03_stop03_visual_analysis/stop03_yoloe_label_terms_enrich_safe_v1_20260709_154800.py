#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stop03_yoloe_label_terms_enrich_safe_v1_20260709_154800.py

Purpose:
  Enrich visual_label_terms.search_terms_json and embedding_text for Chinese-first search/embedding.

Safety:
  - Reads SQLite DB and optional local prompt registry JSON only.
  - Writes only visual_label_terms search_terms_json / embedding_text / updated_at if the column exists.
  - Does not read original media, does not modify source_assets/visual_units/visual_labels/derived_assets.
  - No network, no downloads, no model loading.

Expected DB:
  /Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

SCRIPT_VERSION = "stop03_yoloe_label_terms_enrich_safe_v1_20260709_154800"
DEFAULT_DB = "/Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite"
DEFAULT_REGISTRY = "/Users/yourname/Documents/本地素材大整理配置/提示词注册表/当前提示词_OCR_TRIGGER_v1.0.json"
DEFAULT_OUT = "/Users/yourname/Documents/AI-Local/test-output/stop03-yoloe-label-terms-enrich-safe-v1_20260709_154800"

# Chinese-first query expansion. These are search synonyms, NOT YOLOE model prompts.
# Keep English labels too for bilingual retrieval.
EXTRA_SYNONYMS: Dict[str, List[str]] = {
    # people
    "person": ["人", "人物", "人像", "人影", "路人", "行人", "有人", "人群", "男人", "女人", "老人", "小孩", "孩子", "大人", "村民", "游客", "工作人员", "拍摄对象", "person", "people", "human"],
    "face": ["人脸", "脸", "脸部", "面部", "正脸", "侧脸", "face"],

    # vehicles
    "car": ["车", "小汽车", "汽车", "轿车", "车子", "车辆", "私家车", "家用车", "乘用车", "路上的车", "car", "vehicle"],
    "truck": ["货车", "卡车", "大车", "运输车", "厢货", "货运车", "工程车", "truck", "lorry", "vehicle"],
    "bus": ["公交车", "大巴", "客车", "巴士", "班车", "bus", "coach", "vehicle"],
    "motorcycle": ["摩托车", "机车", "摩托", "两轮摩托", "motorcycle", "motorbike", "vehicle"],
    "electric bike": ["电动车", "电瓶车", "两轮电动车", "电动自行车", "骑电动车", "electric bike", "e-bike", "vehicle"],
    "bicycle": ["自行车", "单车", "脚踏车", "骑车", "骑自行车", "bicycle", "bike", "vehicle"],
    "train": ["火车", "高铁", "动车", "列车", "铁路", "train", "high-speed train"],
    "airplane": ["飞机", "客机", "航班", "机场飞机", "airplane", "plane", "aircraft"],
    "boat": ["船", "小船", "船只", "游船", "渔船", "水上的船", "boat", "ship"],
    "tractor": ["拖拉机", "农用车", "农机", "农业机械", "耕地车", "tractor"],
    "harvester": ["收割机", "联合收割机", "农机", "农业机械", "收麦机", "割麦机", "麦收机器", "harvester", "combine harvester"],
    "license plate": ["车牌", "牌照", "汽车牌照", "车牌号", "license plate"],

    # crops / rural
    "wheat": ["小麦", "麦子", "麦田", "麦穗", "麦地", "成熟的小麦", "金黄色小麦", "wheat"],
    "corn": ["玉米", "玉米地", "玉米苗", "玉米秆", "corn", "maize"],
    "crop": ["农作物", "庄稼", "作物", "庄稼地", "田里的作物", "农田作物", "crop", "crops"],
    "field": ["田地", "地里", "田里", "田野", "庄稼地", "农地", "地头", "field"],
    "farmland": ["农田", "田地", "地里", "庄稼地", "耕地", "田野", "农村地块", "farmland", "field"],
    "grass": ["草", "草地", "草丛", "野草", "草坪", "grass"],
    "tree": ["树", "树木", "树枝", "树林", "树荫", "tree"],
    "forest": ["森林", "树林", "林子", "林地", "forest", "woods"],
    "river": ["河", "河流", "河边", "河道", "水边", "river"],
    "lake": ["湖", "湖面", "湖边", "水面", "lake"],
    "sea": ["海", "海边", "大海", "海面", "sea", "ocean"],
    "mountain": ["山", "山体", "山坡", "山里", "远山", "mountain"],

    # buildings / place
    "building": ["建筑", "楼", "楼房", "建筑物", "房屋建筑", "building"],
    "house": ["房子", "房屋", "住宅", "民房", "家", "老屋", "新屋", "house", "home"],
    "village": ["村庄", "村子", "农村", "村里", "村口", "乡村", "village"],
    "street": ["街道", "街上", "街头", "城市街道", "路边", "street"],
    "road": ["道路", "马路", "公路", "路", "乡村道路", "road"],
    "bridge": ["桥", "桥梁", "桥上", "bridge"],
    "school": ["学校", "校园", "教学楼", "school"],
    "hotel": ["酒店", "宾馆", "旅馆", "hotel"],
    "restaurant": ["餐馆", "饭店", "餐厅", "吃饭的地方", "restaurant"],
    "station": ["车站", "站台", "火车站", "汽车站", "station"],

    # indoor / objects
    "chair": ["椅子", "座椅", "凳子", "椅凳", "chair"],
    "table": ["桌子", "桌面", "饭桌", "工作台", "table"],
    "door": ["门", "门口", "房门", "大门", "door"],
    "window": ["窗", "窗户", "窗边", "window"],
    "bed": ["床", "床铺", "卧室床", "bed"],
    "elevator": ["电梯", "升降梯", "电梯间", "elevator", "lift"],
    "television": ["电视", "电视机", "电视屏幕", "television", "tv"],
    "computer": ["电脑", "计算机", "笔记本电脑", "台式机", "computer", "pc", "laptop"],
    "screen": ["屏幕", "显示屏", "电子屏", "屏幕画面", "screen", "display"],
    "phone": ["手机", "电话", "智能手机", "iPhone", "安卓手机", "phone", "smartphone", "iPhone"],
    "camera": ["相机", "摄影机", "摄像机", "拍摄设备", "镜头", "camera"],

    # text / OCR triggers
    "sign": ["招牌", "标牌", "指示牌", "路牌", "牌子", "标识", "有字的牌子", "sign", "signboard"],
    "logo": ["商标", "标志", "logo", "品牌标志", "图标"],
    "text": ["文字", "字", "字幕", "画面文字", "屏幕文字", "可读文字", "text"],
    "book": ["书", "书本", "书籍", "册子", "book"],
    "paper": ["纸", "纸张", "文件纸", "资料", "paper"],
    "poster": ["海报", "宣传海报", "张贴画", "poster"],
    "billboard": ["广告牌", "户外广告", "大牌子", "billboard"],
    "notice": ["通知", "公告", "告示", "notice"],
    "map": ["地图", "导览图", "路线图", "map"],
    "menu": ["菜单", "菜谱", "点菜单", "menu"],
    "ticket": ["票据", "车票", "门票", "小票", "ticket"],
    "receipt": ["收据", "小票", "购物小票", "receipt"],
    "document": ["文档", "文件", "资料", "纸质文件", "document"],
    "invoice": ["发票", "invoice"],
    "form": ["表格", "表单", "登记表", "form"],
    "presentation slide": ["PPT", "演示文稿", "幻灯片", "课件", "presentation slide", "slide"],
    "webpage": ["网页", "网站页面", "浏览器页面", "webpage", "website"],
    "chat screenshot": ["聊天截图", "聊天记录截图", "微信截图", "聊天界面", "chat screenshot"],
    "screenshot": ["截图", "截屏", "屏幕截图", "screenshot"],
    "screen recording": ["录屏", "屏幕录制", "屏幕录像", "录屏画面", "screen recording"],
    "phone screen": ["手机屏幕", "手机画面", "手机截图", "phone screen"],
    "computer screen": ["电脑屏幕", "电脑画面", "computer screen"],
    "tablet screen": ["平板屏幕", "iPad屏幕", "tablet screen"],
    "laptop screen": ["笔记本电脑屏幕", "laptop screen"],
    "television screen": ["电视屏幕", "television screen"],
    "monitor": ["显示器", "电脑显示器", "monitor"],
    "whiteboard": ["白板", "whiteboard"],
    "blackboard": ["黑板", "blackboard"],
    "label": ["标签", "贴纸标签", "物品标签", "label"],
    "package label": ["包装标签", "快递标签", "商品标签", "package label"],
    "subtitle": ["字幕", "画面字幕", "视频字幕", "subtitle"],

    # animals
    "dog": ["狗", "小狗", "狗狗", "犬", "dog"],
    "cat": ["猫", "小猫", "猫咪", "cat"],
    "bird": ["鸟", "飞鸟", "鸟类", "bird"],
    "chicken": ["鸡", "家鸡", "公鸡", "母鸡", "chicken"],
}


def uniq(seq: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for x in seq:
        if x is None:
            continue
        s = str(x).strip()
        if not s:
            continue
        # Split common slash zh labels, e.g. "车 / 小汽车".
        pieces = [s]
        if " / " in s:
            pieces.extend([p.strip() for p in s.split("/")])
        for p in pieces:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                out.append(p)
    return out


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_query_mappings(registry: Dict[str, Any]) -> Dict[str, List[str]]:
    # registry query_mappings is zh/en query -> list of canonical labels.
    reverse: Dict[str, List[str]] = {}
    for query, labels in (registry.get("query_mappings") or {}).items():
        if not isinstance(labels, list):
            continue
        for label in labels:
            reverse.setdefault(str(label), []).append(str(query))
    return reverse


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def ensure_updated_at_column(con: sqlite3.Connection) -> None:
    cols = table_columns(con, "visual_label_terms")
    if "updated_at" not in cols:
        con.execute("ALTER TABLE visual_label_terms ADD COLUMN updated_at TEXT")


def parse_terms_json(s: str | None) -> List[str]:
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, list):
            return [str(x) for x in v]
    except Exception:
        pass
    return []


def build_embedding_text(label: str, label_zh: str, category_zh: str, source_layer: str, trigger_strength: str, terms: List[str]) -> str:
    parts = []
    if label_zh:
        parts.append(f"中文标签：{label_zh}")
    parts.append(f"英文标签：{label}")
    if category_zh:
        parts.append(f"类别：{category_zh}")
    if terms:
        parts.append("检索词：" + "、".join(terms))
    if "OCR_TRIGGER" in (source_layer or ""):
        parts.append("OCR触发：" + (trigger_strength or "routing"))
    return "。".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    registry_path = Path(args.registry)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        print(json.dumps({"validation_status": "FAIL", "error": f"DB not found: {db_path}"}, ensure_ascii=False, indent=2))
        return 2
    if not registry_path.exists():
        print(json.dumps({"validation_status": "FAIL", "error": f"Registry not found: {registry_path}"}, ensure_ascii=False, indent=2))
        return 2

    registry = load_json(registry_path)
    reverse_query = load_query_mappings(registry)

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "visual_label_terms" not in tables:
        print(json.dumps({"validation_status": "FAIL", "error": "visual_label_terms table not found. Run YOLOE V3/V4 first."}, ensure_ascii=False, indent=2))
        return 2

    if not args.dry_run:
        ensure_updated_at_column(con)

    rows = con.execute("SELECT * FROM visual_label_terms ORDER BY label").fetchall()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = 0
    unchanged = 0
    report_rows = []

    for r in rows:
        label = str(r["label"])
        label_zh = str(r["label_zh"] or "")
        category_zh = str(r["category_zh"] or "")
        source_layer = str(r["source_layer"] or "")
        trigger_strength = str(r["trigger_strength"] or "")
        old_terms = parse_terms_json(r["search_terms_json"] if "search_terms_json" in r.keys() else None)
        extra = EXTRA_SYNONYMS.get(label, [])
        query_terms = reverse_query.get(label, [])
        terms = uniq([label, label_zh] + old_terms + extra + query_terms)
        embedding_text = build_embedding_text(label, label_zh, category_zh, source_layer, trigger_strength, terms)
        new_json = json.dumps(terms, ensure_ascii=False)
        old_json = r["search_terms_json"] if "search_terms_json" in r.keys() else ""
        old_embedding = r["embedding_text"] if "embedding_text" in r.keys() else ""

        is_changed = (new_json != old_json) or (embedding_text != old_embedding)
        if is_changed:
            changed += 1
            if not args.dry_run:
                con.execute(
                    "UPDATE visual_label_terms SET search_terms_json=?, embedding_text=?, updated_at=? WHERE label=?",
                    (new_json, embedding_text, now, label),
                )
        else:
            unchanged += 1
        report_rows.append({
            "label": label,
            "label_zh": label_zh,
            "source_layer": source_layer,
            "trigger_strength": trigger_strength,
            "old_terms_count": len(old_terms),
            "new_terms_count": len(terms),
            "changed": int(is_changed),
            "search_terms_json": new_json,
            "embedding_text": embedding_text,
        })

    if not args.dry_run:
        con.commit()

    csv_path = reports_dir / "visual_label_terms_enriched.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(report_rows[0].keys()) if report_rows else ["label"])
        w.writeheader()
        w.writerows(report_rows)

    # Verify core labels after update
    sample_labels = ["car", "truck", "bus", "motorcycle", "electric bike", "bicycle", "person", "harvester", "tractor", "wheat", "field", "farmland", "crop", "screen recording", "phone screen", "sign"]
    placeholders = ",".join(["?"] * len(sample_labels))
    samples = [dict(x) for x in con.execute(
        f"SELECT label,label_zh,source_layer,trigger_strength,search_terms_json,embedding_text FROM visual_label_terms WHERE label IN ({placeholders}) ORDER BY label",
        sample_labels,
    ).fetchall()]

    summary = {
        "validation_status": "PASS_DRY_RUN" if args.dry_run else "PASS",
        "script_version": SCRIPT_VERSION,
        "db_path": str(db_path),
        "registry_path": str(registry_path),
        "registry_exists": registry_path.exists(),
        "source_safety": "does_not_read_original_media; only updates visual_label_terms search metadata",
        "network": "not used; no downloads; no model loading",
        "dry_run": bool(args.dry_run),
        "counts": {
            "term_rows_seen": len(rows),
            "changed_rows": changed,
            "unchanged_rows": unchanged,
        },
        "outputs": {
            "report_csv": str(csv_path),
        },
        "sample_terms": samples,
    }
    summary_path = reports_dir / "visual_label_terms_enrich_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
