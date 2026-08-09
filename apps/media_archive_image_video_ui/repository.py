from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


VISIBLE_MEDIA_TYPES = ("image", "video")

# Measured defaults from the frozen local validation runs.  These are rates,
# not project totals: a new library always supplies its own item count.
def _int(value: Any) -> int:
    return int(value or 0)


class ReadonlyMediaRepository:
    """Small read-only view over the central SQLite database.

    A new SQLite connection is opened per operation.  This avoids sharing a
    connection with model workers and makes the UI safe while a pipeline run is
    writing short transactions.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        if not self.db_path.is_file():
            raise FileNotFoundError(f"central database not found: {self.db_path}")

    def connect(self) -> sqlite3.Connection:
        # Prefer a normal WAL-aware readonly URI for active tasks.  A finished
        # library on an external/APFS volume can reject SQLite's lock sidecar;
        # immutable mode is then safe because this repository is query-only.
        errors: list[str] = []
        for suffix in ("mode=ro", "mode=ro&immutable=1"):
            try:
                con = sqlite3.connect(
                    f"{self.db_path.as_uri()}?{suffix}", uri=True, timeout=5.0,
                )
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA query_only=ON")
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                return con
            except sqlite3.Error as exc:
                errors.append(str(exc))
        raise sqlite3.OperationalError("readonly_database_open_failed:" + " | ".join(errors))

    @staticmethod
    def _table_exists(con: sqlite3.Connection, table: str) -> bool:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (table,),
        ).fetchone() is not None

    @staticmethod
    def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
        return any(str(row[1]) == column for row in con.execute(f"PRAGMA table_info({table})"))

    @staticmethod
    def _one(con: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> int:
        row = con.execute(sql, tuple(params)).fetchone()
        return _int(row[0] if row else 0)

    def overview(self) -> dict[str, Any]:
        with self.connect() as con:
            source_rows = con.execute(
                """SELECT media_type,COUNT(*) AS n,COALESCE(SUM(size_bytes),0) AS bytes
                   FROM source_assets WHERE media_type IN ('image','video')
                   AND COALESCE(is_deleted_or_missing,0)=0 GROUP BY media_type"""
            ).fetchall() if self._table_exists(con, "source_assets") else []
            source = {str(row["media_type"]): {"count": _int(row["n"]), "bytes": _int(row["bytes"])} for row in source_rows}
            for media_type in VISIBLE_MEDIA_TYPES:
                source.setdefault(media_type, {"count": 0, "bytes": 0})

            visual_rows = con.execute(
                """SELECT s.media_type,COUNT(*) AS n
                   FROM visual_units v JOIN source_assets s USING(source_content_id)
                   WHERE s.media_type IN ('image','video') GROUP BY s.media_type"""
            ).fetchall() if self._table_exists(con, "visual_units") and self._table_exists(con, "source_assets") else []
            visual = {str(row["media_type"]): _int(row["n"]) for row in visual_rows}
            for media_type in VISIBLE_MEDIA_TYPES:
                visual.setdefault(media_type, 0)

            errors = 0
            if self._table_exists(con, "processing_errors"):
                if self._column_exists(con, "processing_errors", "source_content_id"):
                    errors = self._one(
                        con,
                        """SELECT COUNT(*) FROM processing_errors e
                           LEFT JOIN source_assets s ON s.source_content_id=e.source_content_id
                           WHERE s.media_type IN ('image','video') OR s.media_type IS NULL""",
                    )
                else:
                    # The generic source-scan contract stores item_id/item_path
                    # instead of source_content_id. This app library contains
                    # only the current task, so counting its error rows is the
                    # truthful compatible result.
                    errors = self._one(con, "SELECT COUNT(*) FROM processing_errors")

            openclip = self._one(con, "SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings") if self._table_exists(con, "embeddings") else 0
            yoloe = self._one(con, "SELECT COUNT(DISTINCT visual_unit_id) FROM visual_labels") if self._table_exists(con, "visual_labels") else 0
            latest_qwen = con.execute(
                """SELECT run_id FROM stop03_3_qwenvl_runs WHERE status='success'
                   ORDER BY COALESCE(finished_at,started_at) DESC LIMIT 1"""
            ).fetchone() if self._table_exists(con, "stop03_3_qwenvl_runs") else None
            qwen = self._one(
                con, "SELECT COUNT(*) FROM stop03_3_qwenvl_results WHERE run_id=? AND result_status='success'",
                (latest_qwen[0],),
            ) if latest_qwen else 0
            qwen += self._one(
                con, "SELECT COUNT(DISTINCT candidate_id) FROM stop03_3_qwenvl_supplement_results WHERE result_status='success'"
            ) if self._table_exists(con, "stop03_3_qwenvl_supplement_results") else 0
            ocr = self._one(con, "SELECT COUNT(*) FROM stop03_4_ocr_results WHERE result_status IN ('success','no_text')") if self._table_exists(con, "stop03_4_ocr_results") else 0
            text_vectors = self._one(con, "SELECT COUNT(*) FROM stop03_5d_text_vectors WHERE status='success'") if self._table_exists(con, "stop03_5d_text_vectors") else 0
            duplicate_groups = self._one(con, "SELECT COUNT(*) FROM source_duplicate_groups") if self._table_exists(con, "source_duplicate_groups") else 0
            timelapse_groups = self._one(con, "SELECT COUNT(DISTINCT sequence_id) FROM step02_image_timelapse_keyframes") if self._table_exists(con, "step02_image_timelapse_keyframes") else 0
            latest_candidates = [
                con.execute("SELECT MAX(COALESCE(finished_at,started_at)) FROM model_runs").fetchone()[0]
            ] if self._table_exists(con, "model_runs") else []
            if self._table_exists(con, "stop03_5d_text_embedding_runs"):
                latest_candidates.append(con.execute("SELECT MAX(created_at) FROM stop03_5d_text_embedding_runs").fetchone()[0])
            latest_value = max((str(value) for value in latest_candidates if value), default=None)

        if timelapse_groups == 0:
            # Older completed libraries kept the four-sequence evidence in
            # Step02 derived manifests but did not import it into the optional
            # keyframe table.  Use the same generic compatibility source as
            # the Special Materials page so the overview cannot disagree.
            timelapse_groups = int(
                self._timelapse_manifest_groups(0, 1).get("total") or 0
            )
        usage = shutil.disk_usage(self.db_path.parent)
        return {
            "visible_media_types": list(VISIBLE_MEDIA_TYPES),
            "hidden_media_interfaces": ["audio", "text"],
            "source": source,
            "source_total_count": sum(row["count"] for row in source.values()),
            "source_total_bytes": sum(row["bytes"] for row in source.values()),
            "visual_units": visual,
            "visual_unit_total_count": sum(visual.values()),
            "recognition": {
                "openclip_visual_units": openclip,
                "yoloe_detected_visual_units": yoloe,
                "qwen_success": qwen,
                "ocr_completed": ocr,
                "text_vectors": text_vectors,
            },
            "duplicate_group_count": duplicate_groups,
            "timelapse_group_count": timelapse_groups,
            "processing_error_count": errors,
            "latest_pipeline_activity": latest_value,
            "storage": {"total": usage.total, "used": usage.used, "free": usage.free},
        }

    def pipeline(self) -> dict[str, Any]:
        with self.connect() as con:
            def count(table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
                if not self._table_exists(con, table):
                    return 0
                return self._one(con, f"SELECT COUNT(*) FROM {table} {where}", params)

            source_count = self._one(con, "SELECT COUNT(*) FROM source_assets WHERE media_type IN ('image','video')") if self._table_exists(con, "source_assets") else 0
            visual_count = count("visual_units")
            openclip_count = self._one(con, "SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings") if self._table_exists(con, "embeddings") else 0
            dedup_count = count("visual_identity")
            candidate_count = count("stop03_2_candidate_queue_frozen_v25")
            latest_qwen = con.execute(
                """SELECT run_id FROM stop03_3_qwenvl_runs WHERE status='success'
                   ORDER BY COALESCE(finished_at,started_at) DESC LIMIT 1"""
            ).fetchone() if self._table_exists(con, "stop03_3_qwenvl_runs") else None
            qwen_count = count(
                "stop03_3_qwenvl_results", "WHERE run_id=? AND result_status='success'", (latest_qwen[0],)
            ) if latest_qwen else 0
            qwen_count += count(
                "stop03_3_qwenvl_supplement_results", "WHERE result_status='success'"
            )
            ocr_count = count("stop03_4_ocr_results", "WHERE result_status IN ('success','no_text')")
            document_count = count("stop03_5d_text_documents")
            vector_count = count("stop03_5d_text_vectors", "WHERE status='success'")
            document_link_count = count("stop03_5d_document_vector_links")
            frozen_count = count("pipeline_frozen_contracts", "WHERE status='FROZEN'")
            failed = count("processing_errors")

            stages = [
                ("scan", "素材扫描", source_count, source_count, "建立图片/视频清单与路径关系"),
                ("preview", "预览图与视频关键帧", visual_count, source_count, "生成搜索和识别使用的派生画面"),
                ("dedup", "重复与特殊素材识别", dedup_count, visual_count, "标记重复关系和延时摄影关键帧"),
                ("visual", "全量视觉覆盖", openclip_count, visual_count, "OpenCLIP 覆盖全部可搜索画面"),
                ("semantic", "内容识别", qwen_count + ocr_count, candidate_count, "Qwen-VL 高价值描述与 OCR 文字"),
                ("text", "文本向量", document_link_count, document_count, f"{vector_count} 个不同文本向量供全部文档复用"),
                ("search", "搜索可用性校验", frozen_count, 1, "全视觉、文本和物体标签融合搜索"),
            ]
            result = []
            for key, name, done, total, description in stages:
                if key == "preview":
                    status = "success" if done > 0 else "waiting"
                    percent = 100.0 if done > 0 else 0.0
                elif key == "dedup":
                    status = "success" if done > 0 else "waiting"
                    percent = 100.0 if done > 0 else 0.0
                elif key == "search":
                    status = "success" if done > 0 and openclip_count == visual_count and visual_count > 0 else "review"
                    percent = 100.0 if status == "success" else 0.0
                else:
                    status = "success" if total > 0 and done >= total else ("running" if done > 0 else "waiting")
                    percent = min(100.0, (done / total * 100.0) if total else 0.0)
                result.append({
                    "key": key, "name": name, "status": status,
                    "done": done, "total": total, "percent": round(percent, 2),
                    "description": description,
                })
        return {
            "stages": result,
            "overall_percent": round(sum(row["percent"] for row in result) / len(result), 2),
            "failed_record_count": failed,
            "search_ready": bool(visual_count and openclip_count == visual_count and vector_count),
            "full_pipeline_launcher_status": "READY_STAGE_SERIAL",
        }

    def stage_metrics(self) -> dict[str, dict[str, Any]]:
        """Return truthful item counts for the native 15-stage task view.

        Schema-preparation stages intentionally have ``total=0`` because they
        do not process a countable media queue.  The UI displays their state
        without inventing a misleading ``0/1`` or ``1/1`` counter.
        """
        with self.connect() as con:
            def count(table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
                if not self._table_exists(con, table):
                    return 0
                return self._one(con, f"SELECT COUNT(*) FROM {table} {where}", params)

            source_image = count("source_assets", "WHERE media_type='image'")
            source_video = count("source_assets", "WHERE media_type='video'")
            source_total = source_image + source_video
            image_visual = 0
            video_visual = 0
            video_sources_with_frames = 0
            if self._table_exists(con, "visual_units") and self._table_exists(con, "source_assets"):
                media_rows = con.execute(
                    """SELECT s.media_type,COUNT(*) AS n
                       FROM visual_units v JOIN source_assets s USING(source_content_id)
                       WHERE s.media_type IN ('image','video') GROUP BY s.media_type"""
                ).fetchall()
                media_counts = {str(row["media_type"]): _int(row["n"]) for row in media_rows}
                image_visual = media_counts.get("image", 0)
                video_visual = media_counts.get("video", 0)
                video_sources_with_frames = self._one(
                    con,
                    """SELECT COUNT(DISTINCT v.source_content_id)
                       FROM visual_units v JOIN source_assets s USING(source_content_id)
                       WHERE s.media_type='video'""",
                )
            visual_total = image_visual + video_visual
            openclip = self._one(
                con, "SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings"
            ) if self._table_exists(con, "embeddings") else 0
            yoloe = self._one(
                con, "SELECT COUNT(DISTINCT visual_unit_id) FROM visual_labels"
            ) if self._table_exists(con, "visual_labels") else 0
            yoloe_labels = count("visual_labels")
            yoloe_processed = yoloe
            if self._table_exists(con, "model_runs"):
                completed_yoloe = con.execute(
                    """SELECT input_count FROM model_runs
                       WHERE LOWER(stage) LIKE '%yoloe%'
                         AND LOWER(status) IN ('success','pass','completed','done')
                       ORDER BY COALESCE(finished_at,started_at) DESC LIMIT 1"""
                ).fetchone()
                if completed_yoloe:
                    yoloe_processed = _int(completed_yoloe[0])
            person_reid_done = 0
            person_reid_total = visual_total
            person_reid_faces = 0
            person_reid_clusters = 0
            if self._table_exists(con, "stop03_1c_person_reid_runs"):
                latest_person_run = con.execute(
                    """SELECT run_id,visual_unit_count,face_count,cluster_count
                       FROM stop03_1c_person_reid_runs
                       ORDER BY created_at DESC,run_id DESC LIMIT 1"""
                ).fetchone()
                if latest_person_run:
                    person_reid_total = _int(latest_person_run["visual_unit_count"])
                    person_reid_faces = _int(latest_person_run["face_count"])
                    person_reid_clusters = _int(latest_person_run["cluster_count"])
                    person_reid_done = count(
                        "stop03_1c_person_reid_run_items",
                        "WHERE run_id=? AND status IN ('success','no_face')",
                        (str(latest_person_run["run_id"]),),
                    )
            dedup = count("visual_identity")
            ledger_candidates = count("stop03_2_candidate_queue_items")
            ledger_qwen_candidates = count(
                "stop03_2_candidate_queue_items", "WHERE queue_type='qwenvl_high_value'"
            )
            ledger_ocr_candidates = count(
                "stop03_2_candidate_queue_items", "WHERE queue_type='ocr_trigger'"
            )
            ledger_has_media_type = (
                self._table_exists(con, "stop03_2_candidate_queue_items")
                and self._column_exists(con, "stop03_2_candidate_queue_items", "media_type")
            )
            ledger_qwen_image_candidates = count(
                "stop03_2_candidate_queue_items",
                "WHERE queue_type='qwenvl_high_value' AND media_type='image'",
            ) if ledger_has_media_type else 0
            ledger_qwen_video_candidates = count(
                "stop03_2_candidate_queue_items",
                "WHERE queue_type='qwenvl_high_value' AND media_type='video'",
            ) if ledger_has_media_type else max(
                0, ledger_qwen_candidates - ledger_qwen_image_candidates
            )
            ledger_role_counts: dict[str, int] = {}
            if (
                self._table_exists(con, "stop03_2_candidate_queue_items")
                and self._column_exists(con, "stop03_2_candidate_queue_items", "candidate_role")
            ):
                ledger_role_counts = {
                    str(row["candidate_role"]): _int(row["n"])
                    for row in con.execute(
                        "SELECT candidate_role,COUNT(*) AS n "
                        "FROM stop03_2_candidate_queue_items "
                        "WHERE queue_type='qwenvl_high_value' GROUP BY candidate_role"
                    )
                }
            candidates = count("stop03_2_candidate_queue_frozen_v25")
            qwen_candidates = count(
                "stop03_2_candidate_queue_frozen_v25", "WHERE queue_type='qwenvl_high_value'"
            )
            candidate_has_media_type = (
                self._table_exists(con, "stop03_2_candidate_queue_frozen_v25")
                and self._column_exists(con, "stop03_2_candidate_queue_frozen_v25", "media_type")
            )
            qwen_image_candidates = count(
                "stop03_2_candidate_queue_frozen_v25",
                "WHERE queue_type='qwenvl_high_value' AND media_type='image'",
            ) if candidate_has_media_type else 0
            qwen_video_candidates = count(
                "stop03_2_candidate_queue_frozen_v25",
                "WHERE queue_type='qwenvl_high_value' AND media_type='video'",
            ) if candidate_has_media_type else max(0, qwen_candidates - qwen_image_candidates)
            qwen_role_counts: dict[str, int] = {}
            if (
                self._table_exists(con, "stop03_2_candidate_queue_frozen_v25")
                and self._column_exists(con, "stop03_2_candidate_queue_frozen_v25", "candidate_role")
            ):
                qwen_role_counts = {
                    str(row["candidate_role"]): _int(row["n"])
                    for row in con.execute(
                        "SELECT candidate_role,COUNT(*) AS n "
                        "FROM stop03_2_candidate_queue_frozen_v25 "
                        "WHERE queue_type='qwenvl_high_value' GROUP BY candidate_role"
                    )
                }
            ocr_candidates = count(
                "stop03_2_candidate_queue_frozen_v25", "WHERE queue_type='ocr_trigger'"
            )
            qwen_main_done = count("stop03_3_qwenvl_results", "WHERE result_status='success'")
            supplement_candidates = count("stop03_3_qwenvl_supplement_candidates")
            supplement_done = count(
                "stop03_3_qwenvl_supplement_results", "WHERE result_status='success'"
            )
            qwen_all_done = qwen_main_done + supplement_done
            qwen_all_candidates = qwen_candidates + supplement_candidates
            ocr_done = count(
                "stop03_4_ocr_results", "WHERE result_status IN ('success','no_text')"
            )
            evidence = count("stop03_5_unified_evidence_items")
            propagated = count("stop03_5c_propagation_items")
            documents = count("stop03_5d_text_documents")
            vector_links = count("stop03_5d_document_vector_links")
            vector_done = count("stop03_5d_text_vectors", "WHERE status='success'")
            timelapse = self._one(
                con, "SELECT COUNT(DISTINCT sequence_id) FROM step02_image_timelapse_keyframes"
            ) if self._table_exists(con, "step02_image_timelapse_keyframes") else 0
            finder_tags_scanned = self._one(
                con,
                "SELECT COUNT(DISTINCT source_content_id) FROM source_file_records "
                "WHERE finder_tag_status IN ('ok','none')",
            ) if self._table_exists(con, "source_file_records") else 0

        if timelapse == 0:
            timelapse = int(self._timelapse_manifest_groups(0, 100).get("total") or 0)
        missing_video = max(0, source_video - video_sources_with_frames)
        finder_image_candidates = qwen_role_counts.get("image_finder_tag_seed", 0)
        timelapse_image_candidates = qwen_role_counts.get("image_timelapse_representative", 0)
        video_coverage_overlap = qwen_role_counts.get("video_coverage_high_signal_overlap", 0)
        video_coverage_only = qwen_role_counts.get("video_coverage_keyframe", 0)
        video_supplement = qwen_role_counts.get("video_high_signal_supplement", 0)
        candidate_breakdown = (
            f"视频 {qwen_video_candidates} 张"
            f"（覆盖兼高信号 {video_coverage_overlap}、覆盖补位 {video_coverage_only}、高信号补充 {video_supplement}）；"
            f"图片冻结候选 {qwen_image_candidates} 张"
            f"（Finder 标签 {finder_image_candidates}、延时摄影代表帧 {timelapse_image_candidates}）；"
            f"用户选择全部图片时另补充 {supplement_candidates} 张；"
            f"OCR {ocr_candidates} 张"
        )
        ledger_candidate_breakdown = (
            f"视频 {ledger_qwen_video_candidates} 张"
            f"（覆盖兼高信号 {ledger_role_counts.get('video_coverage_high_signal_overlap', 0)}、"
            f"覆盖补位 {ledger_role_counts.get('video_coverage_keyframe', 0)}、"
            f"高信号补充 {ledger_role_counts.get('video_high_signal_supplement', 0)}）；"
            f"图片候选 {ledger_qwen_image_candidates} 张；"
            f"OCR {ledger_ocr_candidates} 张"
        )
        return {
            "scan": {"done": source_total, "total": source_total,
                     "description": f"图片 {source_image} 个，视频 {source_video} 个"},
            "image_preview": {"done": image_visual, "total": image_visual,
                              "description": f"可搜索图片 {image_visual} 张，延时摄影 {timelapse} 组"},
            "video_frames": {"done": video_sources_with_frames, "total": source_video,
                             "description": f"已覆盖 {video_sources_with_frames} 个视频，共生成 {video_visual} 张派生帧；{missing_video} 个视频未生成派生帧"},
            "visual_schema_v3": {"done": 0, "total": 0, "description": "数据库结构准备阶段，无逐项队列"},
            "yoloe": {"done": yoloe_processed, "total": visual_total,
                      "description": f"处理 {yoloe_processed} 张画面，其中 {yoloe} 张检测到物体，写入 {yoloe_labels} 条标签"},
            "openclip": {"done": openclip, "total": visual_total,
                         "description": "覆盖全部可搜索图片与视频派生帧"},
            "rebuild_openclip": {"done": openclip, "total": visual_total,
                                 "description": "仅补齐重扫或重新分组后新增且缺少向量的画面"},
            "dedup": {"done": dedup, "total": visual_total,
                      "description": "逐画面建立来源与近重复关系"},
            "person_reid_optional_v1": {
                "done": person_reid_done, "total": person_reid_total,
                "description": (
                    f"全量检查 {person_reid_done}/{person_reid_total} 张画面；"
                    f"取得 {person_reid_faces} 条合格人脸特征，机器归并为 "
                    f"{person_reid_clusters} 个待确认人物组；不代表真实人数，背影未仅凭服装强行认人"
                ),
            },
            "candidate_schema": {"done": 0, "total": 0, "description": "候选数据库结构准备阶段，无逐项队列"},
            "candidates_generic_v2": {
                "done": ledger_candidates,
                "total": ledger_candidates,
                "description": "候选计算链：" + ledger_candidate_breakdown,
            },
            "candidate_snapshot": {"done": candidates, "total": candidates,
                                   "description": "冻结当前素材库候选快照"},
            "qwen_optional_v2": {"done": qwen_main_done, "total": qwen_candidates,
                                 "description": f"冻结规则高价值画面 {qwen_main_done}/{qwen_candidates} 张；" + candidate_breakdown},
            "all_image_supplement_contract": {"done": supplement_candidates, "total": supplement_candidates,
                                                "description": "按当前数据库图片数量建立补充队列；不写死输出张数"},
            "all_image_supplement_qwen": {"done": supplement_done, "total": supplement_candidates,
                                            "description": f"用户选择全部图片后新增分析 {supplement_done}/{supplement_candidates} 张；全部高价值描述 {qwen_all_done}/{qwen_all_candidates} 张"},
            "all_image_evidence_merge": {"done": evidence, "total": evidence,
                                           "description": "把新增图片描述追加到统一证据；保留原冻结结果"},
            "ocr_optional_v2": {"done": ocr_done, "total": ocr_candidates,
                                "description": f"画面文字识别 {ocr_done}/{ocr_candidates} 张"},
            # The unified evidence table may be appended by the following
            # all-image merge stage.  Do not show an impossible 19 / 7 after
            # the completed task is reopened from history.
            "evidence_optional_v2": {"done": evidence, "total": evidence,
                                     "description": "合并 Qwen-VL 与 OCR 的直接证据"},
            "propagation_optional_v2": {"done": propagated, "total": propagated,
                                        "description": "按通用邻帧规则生成语义传播记录"},
            "embedding_optional_v2": {"done": vector_done, "total": vector_done,
                                      "description": f"{documents} 份可搜索文档全部建立链接；去重后只计算 {vector_done} 个不同文本向量（共 {vector_links} 条文档—向量链接）"},
            "repair_finder_tags": {"done": finder_tags_scanned, "total": source_image,
                                    "description": "只读补查现有图片的 macOS Finder 标签"},
            "repair_candidate_dry_run": {"done": supplement_candidates, "total": supplement_candidates,
                                         "description": "不改写冻结 V25，仅计算新的图片补充候选"},
            "repair_supplement_contract": {"done": supplement_candidates, "total": supplement_candidates,
                                           "description": "追加式补充队列，不改动原候选快照"},
            "repair_supplement_qwen": {"done": supplement_done, "total": supplement_candidates,
                                       "description": f"缺失图片描述 {supplement_done}/{supplement_candidates} 张"},
            "repair_evidence_merge": {"done": evidence, "total": evidence,
                                      "description": "保留旧证据并建立新的合并版本"},
            "repair_propagation": {"done": propagated, "total": propagated,
                                   "description": "OCR 不传播，只更新 Qwen-VL 相关语义"},
            "repair_embedding": {"done": vector_done, "total": vector_done,
                                 "description": f"当前 {documents} 份文本记录、{vector_done} 个唯一向量"},
        }

    def recent_runs(self, limit: int = 40) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        rows: list[dict[str, Any]] = []
        with self.connect() as con:
            if self._table_exists(con, "model_runs"):
                rows.extend(dict(row) | {"kind": "model_run"} for row in con.execute(
                    """SELECT run_id,stage,status,input_count,output_count,started_at,finished_at,error_message
                       FROM model_runs ORDER BY COALESCE(finished_at,started_at) DESC LIMIT ?""", (limit,)
                ))
            for table, run_id_col, stage, count_col in (
                ("stop03_3_qwenvl_runs", "run_id", "Qwen-VL 内容识别", "candidate_count"),
                ("stop03_4_ocr_runs", "run_id", "OCR 文字识别", "candidate_count"),
                ("stop03_5d_text_embedding_runs", "embedding_run_id", "文本向量", "document_count"),
            ):
                if not self._table_exists(con, table):
                    continue
                started = "created_at" if table == "stop03_5d_text_embedding_runs" else "started_at"
                rows.extend(dict(row) | {"kind": table, "stage": stage} for row in con.execute(
                    f"""SELECT {run_id_col} AS run_id,status,{count_col} AS input_count,
                               COALESCE(success_count,0) AS output_count,{started} AS started_at,
                               finished_at,error_message FROM {table}
                           ORDER BY COALESCE(finished_at,{started}) DESC LIMIT ?""", (limit,)
                ))
        rows.sort(key=lambda row: str(row.get("finished_at") or row.get("started_at") or ""), reverse=True)
        return rows[:limit]

    def active_runs(self) -> list[dict[str, Any]]:
        """Return user-facing progress with an ETA derived from completed work.

        No project-specific totals are used.  Every total and completion count
        comes from the active run row in the central database.
        """
        active: list[dict[str, Any]] = []
        with self.connect() as con:
            if self._table_exists(con, "stop03_3_qwenvl_runs"):
                workers = "workers" if self._column_exists(con, "stop03_3_qwenvl_runs", "workers") else "1"
                if self._table_exists(con, "stop03_3_qwenvl_run_items"):
                    qwen_completed = """(
                        SELECT COUNT(*) FROM stop03_3_qwenvl_run_items i
                        WHERE i.run_id=r.run_id AND i.status NOT IN ('pending','running')
                    )"""
                    qwen_pending = """(
                        SELECT COUNT(*) FROM stop03_3_qwenvl_run_items i
                        WHERE i.run_id=r.run_id AND i.status IN ('pending','running')
                    )"""
                else:
                    qwen_completed = "r.success_count+r.failed_count+r.review_count"
                    qwen_pending = "r.pending_count"
                for row in con.execute(
                    f"""SELECT r.run_id,'高价值画面描述（Qwen-VL）' AS stage,r.candidate_count AS total,
                              {qwen_completed} AS completed,
                              {qwen_pending} AS pending,r.started_at,{workers} AS workers,
                              'qwen_vl' AS eta_kind
                       FROM stop03_3_qwenvl_runs r WHERE r.status='running'"""
                ):
                    active.append(dict(row))
            if self._table_exists(con, "stop03_4_ocr_runs"):
                workers = "workers" if self._column_exists(con, "stop03_4_ocr_runs", "workers") else "1"
                if self._table_exists(con, "stop03_4_ocr_run_items"):
                    ocr_completed = """(
                        SELECT COUNT(*) FROM stop03_4_ocr_run_items i
                        WHERE i.run_id=r.run_id AND i.status NOT IN ('pending','running')
                    )"""
                    ocr_pending = """(
                        SELECT COUNT(*) FROM stop03_4_ocr_run_items i
                        WHERE i.run_id=r.run_id AND i.status IN ('pending','running')
                    )"""
                else:
                    ocr_completed = "r.success_count+r.no_text_count+r.failed_count"
                    ocr_pending = "r.pending_count"
                for row in con.execute(
                    f"""SELECT r.run_id,'画面文字识别（OCR）' AS stage,r.candidate_count AS total,
                              {ocr_completed} AS completed,
                              {ocr_pending} AS pending,r.started_at,{workers} AS workers,
                              'ocr' AS eta_kind
                       FROM stop03_4_ocr_runs r WHERE r.status='running'"""
                ):
                    active.append(dict(row))
            if self._table_exists(con, "stop03_5d_text_embedding_runs"):
                workers = "workers" if self._column_exists(con, "stop03_5d_text_embedding_runs", "workers") else "1"
                for row in con.execute(
                    f"""SELECT embedding_run_id AS run_id,'文本搜索向量（Qwen3-Embedding）' AS stage,unique_text_count AS total,
                              success_count+failed_count AS completed,pending_count AS pending,created_at AS started_at,
                              {workers} AS workers,'embedding' AS eta_kind
                       FROM stop03_5d_text_embedding_runs WHERE status='running'"""
                ):
                    active.append(dict(row))
            if self._table_exists(con, "model_runs"):
                for row in con.execute(
                    """SELECT run_id,stage,input_count AS total,output_count AS completed,
                              MAX(input_count-output_count,0) AS pending,started_at,1 AS workers,
                              NULL AS eta_kind
                       FROM model_runs
                       WHERE LOWER(status) IN ('running','pending','queued','retrying','review')"""
                ):
                    active.append(dict(row))
        now = datetime.now(timezone.utc)
        for row in active:
            completed = _int(row.get("completed"))
            total = _int(row.get("total"))
            try:
                started = datetime.fromisoformat(str(row.get("started_at")).replace("Z", "+00:00"))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = max(0.0, (now - started.astimezone(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                elapsed = 0.0
            remaining = max(0, total - completed)
            eta = (
                elapsed / completed * remaining
                if completed >= 20 and elapsed >= 30.0 and remaining > 0
                else None
            )
            row["elapsed_seconds"] = round(elapsed, 1)
            row["remaining"] = remaining
            row["percent"] = round((completed / total * 100.0) if total else 0.0, 2)
            row["eta_seconds"] = round(eta, 1) if eta is not None else None
            row["eta_basis"] = (
                "按本次运行实际吞吐量估算"
                if eta is not None else
                "正在估算；至少完成20项并运行30秒后显示"
            )
        return active

    def duplicate_groups(self, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 100))
        with self.connect() as con:
            if not self._table_exists(con, "source_duplicate_groups"):
                return {"total": 0, "offset": offset, "limit": limit, "items": []}
            real_group_where = """
                WHERE (
                    SELECT COUNT(*)
                    FROM source_asset_identity i
                    JOIN source_file_records mf
                      ON mf.source_file_id=i.source_file_record_id
                    WHERE i.duplicate_group_id=g.duplicate_group_id
                      AND mf.file_name NOT LIKE '._%'
                ) >= 2
            """
            total = self._one(
                con,
                "SELECT COUNT(*) FROM source_duplicate_groups g " + real_group_where,
            )
            rows = con.execute(
                """SELECT g.duplicate_group_id,g.member_count,g.total_bytes,g.canonical_reason,
                          f.source_content_id,f.file_name,f.relative_path,f.size_bytes
                   FROM source_duplicate_groups g
                   LEFT JOIN source_file_records f ON f.source_file_id=g.canonical_source_file_record_id
                   """ + real_group_where + """
                   ORDER BY g.total_bytes DESC,g.duplicate_group_id LIMIT ? OFFSET ?""",
                (limit, offset),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                members = [dict(member) for member in con.execute(
                    """SELECT f.source_file_id,f.source_content_id,f.file_name,
                              f.relative_path,f.absolute_path,f.size_bytes,
                              i.identity_status
                       FROM source_asset_identity i
                       JOIN source_file_records f
                         ON f.source_file_id=i.source_file_record_id
                       WHERE i.duplicate_group_id=?
                         AND f.file_name NOT LIKE '._%'
                       ORDER BY CASE i.identity_status WHEN 'canonical' THEN 0 ELSE 1 END,
                                f.relative_path,f.source_file_id""",
                    (row["duplicate_group_id"],),
                )]
                for member in members:
                    absolute = str(member.get("absolute_path") or "")
                    member["folder_path"] = str(Path(absolute).parent) if absolute else ""
                    member["is_canonical"] = member.get("identity_status") == "canonical"
                item["members"] = members
                item["member_count"] = len(members)
                item["total_bytes"] = sum(int(member.get("size_bytes") or 0) for member in members)
                items.append(item)
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    def timelapse_groups(self, offset: int = 0, limit: int = 30) -> dict[str, Any]:
        offset, limit = max(0, int(offset)), max(1, min(int(limit), 100))
        sequence_metadata = self._timelapse_sequence_metadata()
        with self.connect() as con:
            if not self._table_exists(con, "step02_image_timelapse_keyframes"):
                return self._timelapse_manifest_groups(offset, limit)
            rows = con.execute(
                """SELECT sequence_id,COUNT(*) AS keyframe_count,MIN(source_relative_path) AS first_path,
                          MIN(created_at) AS created_at
                   FROM step02_image_timelapse_keyframes GROUP BY sequence_id
                   ORDER BY sequence_id LIMIT ? OFFSET ?""", (limit, offset)
            ).fetchall()
            total = self._one(con, "SELECT COUNT(DISTINCT sequence_id) FROM step02_image_timelapse_keyframes")
            if total == 0:
                return self._timelapse_manifest_groups(offset, limit)
            items: list[dict[str, Any]] = []
            for group in rows:
                frames = [dict(row) for row in con.execute(
                    """SELECT k.visual_unit_id,k.representative_position,k.source_relative_path,
                              v.derived_id,v.time_position_ms,s.absolute_path AS source_path
                       FROM step02_image_timelapse_keyframes k
                       LEFT JOIN visual_units v ON v.visual_unit_id=k.visual_unit_id
                       LEFT JOIN source_assets s ON s.source_content_id=k.parent_source_content_id
                       WHERE k.sequence_id=?
                       ORDER BY CASE k.representative_position WHEN 'first' THEN 1 WHEN 'middle' THEN 2 ELSE 3 END""",
                    (group["sequence_id"],),
                )]
                item = dict(group)
                item["frames"] = frames
                metadata = sequence_metadata.get(str(group["sequence_id"]), {})
                item.update(metadata)
                item["source_folder"] = self._timelapse_source_folder(frames, metadata)
                items.append(item)
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    def _timelapse_sequence_metadata(self) -> dict[str, dict[str, Any]]:
        """Read generic per-sequence totals from the Step02 derived manifest.

        ``selected_representatives`` is deliberately kept separate from
        ``image_count``: three preview frames never means that the original
        sequence contains only three photos.
        """
        manifest = (
            self.db_path.parent
            / "stages/02_image_preview/manifests/image_timelapse_sequences.csv"
        )
        if not manifest.is_file():
            return {}
        metadata: dict[str, dict[str, Any]] = {}
        with manifest.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                sequence_id = str(row.get("sequence_id") or "").strip()
                if not sequence_id:
                    continue
                try:
                    source_photo_count = int(row.get("image_count") or 0)
                except (TypeError, ValueError):
                    source_photo_count = 0
                metadata[sequence_id] = {
                    "source_photo_count": source_photo_count,
                    "source_relative_dir": str(row.get("relative_dir") or ""),
                    "first_source_relative_path": str(row.get("first_file") or ""),
                    "last_source_relative_path": str(row.get("last_file") or ""),
                    "sequence_start_time": str(row.get("start_time") or ""),
                    "sequence_end_time": str(row.get("end_time") or ""),
                }
        return metadata

    @staticmethod
    def _timelapse_source_folder(
        frames: list[dict[str, Any]], metadata: dict[str, Any],
    ) -> str:
        for frame in frames:
            source_path = str(frame.get("source_path") or "").strip()
            if source_path and Path(source_path).is_absolute():
                return str(Path(source_path).expanduser().parent)
        return ""

    def _timelapse_manifest_groups(self, offset: int, limit: int) -> dict[str, Any]:
        """Compatibility fallback for completed Step02 runs predating DB import.

        The manifest is a pipeline-owned derived artifact beside this task DB;
        original media is never opened.  New pipeline runs should also import
        the same rows into ``step02_image_timelapse_keyframes``.
        """
        manifest = (
            self.db_path.parent
            / "stages/02_image_preview/manifests/image_preview_manifest.csv"
        )
        if not manifest.is_file():
            return {"total": 0, "offset": offset, "limit": limit, "items": []}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        sequence_metadata = self._timelapse_sequence_metadata()
        with manifest.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                sequence_id = str(row.get("sequence_id") or "").strip()
                output = Path(str(row.get("output_path") or "")).expanduser()
                if (
                    not sequence_id
                    or str(row.get("status") or "") != "success"
                    or not output.is_file()
                ):
                    continue
                groups[sequence_id].append({
                    "visual_unit_id": "",
                    "derived_id": "",
                    "time_position_ms": None,
                    "representative_position": row.get("representative_position") or "",
                    "source_relative_path": row.get("source_relative_path") or "",
                    "source_path": row.get("source_path") or row.get("parent_source_path_at_processing_time") or "",
                    "preview_path": str(output.resolve()),
                })
        ordered = sorted(groups, key=lambda value: (len(value), value))
        selected = ordered[offset:offset + limit]
        role_order = {"first": 1, "middle": 2, "last": 3}
        items = []
        for sequence_id in selected:
            frames = sorted(
                groups[sequence_id],
                key=lambda row: role_order.get(str(row["representative_position"]), 9),
            )
            items.append({
                "sequence_id": sequence_id,
                "keyframe_count": len(frames),
                "first_path": frames[0]["source_relative_path"] if frames else "",
                "created_at": None,
                "frames": frames,
                **sequence_metadata.get(sequence_id, {}),
                "source_folder": self._timelapse_source_folder(
                    frames, sequence_metadata.get(sequence_id, {}),
                ),
                "source": "step02_derived_manifest_fallback",
            })
        return {
            "total": len(ordered), "offset": offset, "limit": limit,
            "items": items, "source": "step02_derived_manifest_fallback",
        }

    def derived_path(self, derived_id: str) -> Path | None:
        with self.connect() as con:
            row = con.execute("SELECT derived_path FROM derived_assets WHERE derived_id=?", (derived_id,)).fetchone()
        if not row:
            return None
        path = Path(str(row[0])).expanduser().resolve()
        return path if path.is_file() else None

    def source_media(self, source_content_id: str) -> dict[str, Any] | None:
        with self.connect() as con:
            row = con.execute(
                """SELECT source_content_id,absolute_path,relative_path,file_name,extension,media_type,size_bytes
                   FROM source_assets WHERE source_content_id=? AND media_type IN ('image','video')""",
                (source_content_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        path = Path(str(item["absolute_path"])).expanduser().resolve()
        item["resolved_path"] = str(path)
        item["available"] = path.is_file()
        return item

    def person_clusters_for_visual_units(
        self, visual_unit_ids: Iterable[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Return conservative anonymous-person links for displayed results.

        Only multi-frame high-confidence clusters (or human-confirmed clusters)
        are exposed.  Older libraries without the optional ReID schema simply
        return no links.
        """
        identifiers = sorted({str(value) for value in visual_unit_ids if value})
        if not identifiers:
            return {}
        with self.connect() as con:
            if not self._table_exists(
                con, "v_stop03_1c_latest_person_cluster_members",
            ):
                return {}
            placeholders = ",".join("?" for _ in identifiers)
            rows = con.execute(
                f"""
                SELECT visual_unit_id,person_cluster_id,member_count,
                       distinct_source_count,cluster_confidence,
                       human_review_status,anonymous_display_name
                FROM v_stop03_1c_latest_person_cluster_members
                WHERE visual_unit_id IN ({placeholders})
                  AND member_count > 1
                  AND (
                    distinct_source_count >= 2
                    OR human_review_status='confirmed'
                  )
                  AND (
                    cluster_confidence='high'
                    OR human_review_status='confirmed'
                  )
                ORDER BY visual_unit_id,member_count DESC,person_cluster_id
                """,
                identifiers,
            ).fetchall()
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for row in rows:
            visual_id = str(row["visual_unit_id"])
            cluster_id = str(row["person_cluster_id"])
            key = (visual_id, cluster_id)
            if key in seen:
                continue
            seen.add(key)
            result[visual_id].append({
                "person_cluster_id": cluster_id,
                "member_count": _int(row["member_count"]),
                "distinct_source_count": _int(row["distinct_source_count"]),
                "cluster_confidence": str(row["cluster_confidence"]),
                "human_review_status": str(row["human_review_status"]),
                "display_name": str(row["anonymous_display_name"] or "同一匿名人物"),
            })
        return dict(result)

    def person_cluster_results(
        self,
        person_cluster_id: str | Sequence[str],
        media_type: str = "all",
        offset: int = 0,
        limit: int = 30,
        source_content_id: str | None = None,
    ) -> dict[str, Any]:
        """Read one anonymous-person cluster without running a model."""
        cluster_ids = (
            sorted({str(value).strip() for value in person_cluster_id if str(value).strip()})
            if not isinstance(person_cluster_id, str)
            else [person_cluster_id.strip()]
        )
        if not cluster_ids or not cluster_ids[0]:
            raise ValueError("person_cluster_id_required")
        if media_type not in {"all", "image", "video"}:
            raise ValueError("person_cluster_media_type_invalid")
        with self.connect() as con:
            if not self._table_exists(
                con, "v_stop03_1c_latest_person_cluster_members",
            ):
                return {"total": 0, "items": [], "count_by_media": {}}
            placeholders = ",".join("?" for _ in cluster_ids)
            rows = con.execute(
                f"""
                SELECT p.person_cluster_id,p.member_count,
                       p.distinct_source_count,p.cluster_confidence,
                       p.human_review_status,p.visual_unit_id,
                       p.source_content_id,p.derived_id,p.media_type,
                       p.time_position_ms,p.similarity_to_representative,
                       d.derived_path,s.absolute_path,s.relative_path
                FROM v_stop03_1c_latest_person_cluster_members AS p
                JOIN derived_assets AS d ON d.derived_id=p.derived_id
                JOIN source_assets AS s
                  ON s.source_content_id=p.source_content_id
                WHERE p.person_cluster_id IN ({placeholders})
                  AND p.member_count > 1
                  AND (
                    p.distinct_source_count >= 2
                    OR p.human_review_status='confirmed'
                  )
                  AND (
                    p.cluster_confidence='high'
                    OR p.human_review_status='confirmed'
                  )
                ORDER BY p.similarity_to_representative DESC,
                         p.time_position_ms,p.visual_unit_id
                """,
                cluster_ids,
            ).fetchall()
        unique: list[dict[str, Any]] = []
        seen_visual_units: set[str] = set()
        count_by_media: dict[str, int] = defaultdict(int)
        for row in rows:
            visual_id = str(row["visual_unit_id"])
            row_media_type = str(row["media_type"])
            if visual_id in seen_visual_units:
                continue
            seen_visual_units.add(visual_id)
            if media_type != "all" and row_media_type != media_type:
                continue
            count_by_media[row_media_type] += 1
            preview_path = Path(str(row["derived_path"])).expanduser().resolve()
            source_path = Path(str(row["absolute_path"])).expanduser().resolve()
            unique.append({
                **dict(row),
                "preview_path": str(preview_path) if preview_path.is_file() else "",
                "source_path": str(source_path) if source_path.is_file() else "",
                "source_online": source_path.is_file(),
                "can_open_original": source_path.is_file(),
            })
        frame_total = len(unique)
        selected: list[dict[str, Any]]
        clean_source = str(source_content_id or "").strip()
        if clean_source:
            selected = [
                dict(row) | {"source_frame_count": 1, "result_level": "frame"}
                for row in unique if str(row.get("source_content_id") or "") == clean_source
            ]
            count_by_media = defaultdict(int)
            for row in selected:
                count_by_media[str(row.get("media_type") or "unknown")] += 1
        else:
            grouped: dict[str, dict[str, Any]] = {}
            counts: dict[str, int] = defaultdict(int)
            for row in unique:
                source_id = str(row.get("source_content_id") or "")
                counts[source_id] += 1
                grouped.setdefault(source_id, row)
            selected = [
                dict(row) | {
                    "source_frame_count": counts[source_id],
                    "result_level": "source",
                }
                for source_id, row in grouped.items()
            ]
            count_by_media = defaultdict(int)
            for row in selected:
                count_by_media[str(row.get("media_type") or "unknown")] += 1
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(int(limit), 100))
        return {
            "total": len(selected),
            "frame_total": frame_total,
            "items": selected[safe_offset:safe_offset + safe_limit],
            "count_by_media": dict(count_by_media),
            "offset": safe_offset,
            "limit": safe_limit,
            "next_offset": (
                safe_offset + safe_limit
                if safe_offset + safe_limit < len(selected)
                else None
            ),
        }

    def person_cluster_catalog(
        self, offset: int = 0, limit: int = 100,
    ) -> dict[str, Any]:
        """List reliable anonymous-person clusters for an explicit UI picker.

        The representative image is an existing derived asset.  This method is
        query-only and deliberately excludes singleton/review-only clusters so
        the UI never presents an uncertain grouping as confirmed.
        """
        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(int(limit), 200))
        with self.connect() as con:
            if not self._table_exists(
                con, "v_stop03_1c_latest_person_cluster_members",
            ):
                return {
                    "total": 0, "offset": safe_offset, "limit": safe_limit,
                    "items": [],
                }
            rows = con.execute(
                """
                SELECT p.person_cluster_id,p.member_count,
                       p.distinct_source_count,p.cluster_confidence,
                       p.human_review_status,p.anonymous_display_name,
                       p.visual_unit_id,p.source_content_id,p.derived_id,
                       p.media_type,p.time_position_ms,d.derived_path,
                       s.absolute_path,s.relative_path
                FROM v_stop03_1c_latest_person_cluster_members AS p
                JOIN derived_assets AS d ON d.derived_id=p.derived_id
                JOIN source_assets AS s
                  ON s.source_content_id=p.source_content_id
                WHERE p.face_id=p.representative_face_id
                  AND p.member_count > 1
                  AND (
                    p.distinct_source_count >= 2
                    OR p.human_review_status='confirmed'
                  )
                  AND (
                    p.cluster_confidence='high'
                    OR p.human_review_status='confirmed'
                  )
                ORDER BY p.member_count DESC,p.distinct_source_count DESC,
                         p.person_cluster_id
                """
            ).fetchall()
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            cluster_id = str(row["person_cluster_id"])
            if cluster_id in seen:
                continue
            seen.add(cluster_id)
            preview = Path(str(row["derived_path"])).expanduser().resolve()
            source = Path(str(row["absolute_path"])).expanduser().resolve()
            items.append({
                **dict(row),
                "preview_path": str(preview) if preview.is_file() else "",
                "source_path": str(source) if source.is_file() else "",
                "source_online": source.is_file(),
            })
        return {
            "total": len(items),
            "offset": safe_offset,
            "limit": safe_limit,
            "items": items[safe_offset:safe_offset + safe_limit],
        }

    def integrity(self) -> dict[str, Any]:
        with self.connect() as con:
            integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = len(con.execute("PRAGMA foreign_key_check").fetchall())
        return {"integrity_check": integrity, "foreign_key_error_count": foreign_keys}
