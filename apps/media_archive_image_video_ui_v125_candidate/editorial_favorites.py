"""Browse existing favorites as manual editorial candidates, without reranking."""
from pathlib import Path

from .central_database import task_id_for_database
from .editorial_assistant import _compact_candidate
from .editorial_candidate.db_adapter import connect_readonly, _candidate_from_row, _tables


def editorial_search_candidate(database: Path, board_database: Path, source_id: str, visual_id: str) -> dict:
    """Resolve the exact clicked search frame, never a nearest/representative replacement.

    Search visual IDs are not editorial document IDs. Reuse an exact document
    when present; otherwise retain the exact existing visual ID in a namespaced
    manual candidate, resolved read-only by the same preview/export adapter.
    This command reads neither original media nor models and writes no database.
    """
    if database.resolve() != board_database.resolve():
        raise ValueError("素材库已切换，请连接选片工程对应的素材库；原选择不变。")
    if any(not value.strip() or len(value) > 200 for value in (source_id, visual_id)):
        raise ValueError("缺少可核对的画面编号；请浏览该视频全部画面后选择具体帧。")
    with connect_readonly(database) as con:
        if not {"visual_units", "derived_assets", "source_assets", "stop03_5d_text_documents"} <= _tables(con):
            raise ValueError("当前库缺少已有画面索引，不能直接补选；不会重新分析素材。")
        row = con.execute("""SELECT d.*, a.derived_path, v.visual_unit_id AS canonical_visual_unit_id,
            NULL AS source_extension, NULL AS source_size_bytes, NULL AS preview_width, NULL AS preview_height
            FROM visual_units v JOIN derived_assets a USING(derived_id)
            JOIN source_assets s ON s.source_content_id=v.source_content_id
            JOIN stop03_5d_text_documents d ON d.derived_id=v.derived_id AND d.source_content_id=v.source_content_id
            WHERE v.visual_unit_id=? AND v.source_content_id=? AND COALESCE(s.is_deleted_or_missing,0)=0
              AND d.quality_status='PASS' AND d.media_type IN ('image','video')
            ORDER BY d.created_at DESC,d.document_id LIMIT 1""", (visual_id, source_id)).fetchone()
        if row is None:
            # Some existing search frames have no text-embedding document. This
            # is NOT grounds to forbid a human choice or silently substitute a
            # different frame. Keep the canonical visual ID; do not write a doc.
            row = con.execute("""SELECT v.visual_unit_id,v.derived_id,v.source_content_id,
                s.relative_path,s.media_type,a.derived_path,
                CASE WHEN v.time_position_ms>=0 THEN v.time_position_ms ELSE a.time_position_ms END AS anchor
                FROM visual_units v JOIN derived_assets a USING(derived_id)
                JOIN source_assets s ON s.source_content_id=v.source_content_id
                WHERE v.visual_unit_id=? AND v.source_content_id=? AND COALESCE(s.is_deleted_or_missing,0)=0
                AND s.media_type IN ('image','video') LIMIT 1""", (visual_id, source_id)).fetchone()
            if row is None:
                raise ValueError("找不到所点击帧的原文件与截图记录，未加入，也不会偷偷换成其他帧。")
            if row['media_type'] == 'video' and (row['anchor'] is None or row['anchor'] < 0):
                raise ValueError("此截图缺少可核对的原片时间点，不能猜测剪点；请另选一帧。")
            row = dict(document_id='manual-visual::'+visual_id, source_content_id=source_id,
                canonical_visual_unit_id=visual_id, source_relative_path=row['relative_path'],
                media_type=row['media_type'], time_position_ms=row['anchor'], derived_path=row['derived_path'],
                document_kind='existing_visual_manual', qwen_text='', ocr_text='', propagated_labels_json='[]',
                source_extension=None, source_size_bytes=None, preview_width=None, preview_height=None)
        raw = _candidate_from_row(row)
        if raw['candidate_id'].startswith('manual-visual::'):
            raw.update(role='人工待定', display_title='人工选择的已有截图（暂无选片文字描述）',
                       observations=['人工选择的已有截图（暂无选片文字描述）'],
                       evidence=['数据库已有截图编号、原文件关联与时间点；不是新分析结果'])
    candidate = _compact_candidate(raw, "search_manual")
    candidate.update(
        recommendation="人工搜索补选", gate_status="SOFT_GATE", gate_reason_codes=["USER_SEARCH_MANUAL"],
        gate_reasons=["用户从搜索结果指定此帧，尚未自动判断与本句的适合度"], requires_source_review=True,
        match_reasons=["精确核对当前素材库、原文件编号和所点击画面对应的派生帧"],
        fit_reason="你从搜索结果人工补选的画面，不代表算法已确认适合本句。",
        recommendation_reason="由用户从当前素材库搜索或逐帧浏览中手动补选；用途由用户复核。",
        rank_reason="人工指定，不参与本句的算法名次比较。",
        provisional_in_ms=raw["start_ms"], provisional_out_ms=raw["end_ms"],
        duration_reason="按所选帧前后各2秒暂存（图片暂定5秒），不是搜索播放器窗口或最终剪点；可回候选箱播放、调整并锁定。")
    return dict(status="PASS", database=str(database.resolve()), candidate=candidate,
                visual_unit_id=visual_id, database_write=False, model_run=False, original_media_read=False)


def editorial_favorites(database: Path, board_database: Path, source_id: str = "", offset: int = 0) -> dict:
    if database.resolve() != board_database.resolve():
        raise ValueError("素材库已切换，请回到选片工程对应的素材库；原选择不变。")
    if offset < 0 or len(source_id) > 200:
        raise ValueError("收藏分页参数无效")
    task_id = task_id_for_database(database)
    result = dict(status="PASS", database=str(database.resolve()), sources=[], candidates=[],
                  offset=offset, next_offset=None, total_frames=0, database_write=False, model_run=False,
                  original_media_read=False, message="收藏是原文件级标记；选片仍需查看具体画面，不代表系统已判断适合本句。")
    with connect_readonly(database) as con:
        tables = _tables(con)
        if "user_asset_annotations" not in tables:
            result["message"] = "当前素材库没有收藏记录；收藏按素材库保存，其他库的收藏不会自动混入。"
            return result
        rows = con.execute("""SELECT a.source_content_id,s.relative_path,a.note
            FROM user_asset_annotations a JOIN source_assets s USING(source_content_id)
            WHERE a.task_id=? AND a.favorite=1 AND s.media_type IN ('image','video')
              AND COALESCE(s.is_deleted_or_missing,0)=0
            ORDER BY a.updated_at DESC,a.source_content_id""", (task_id,)).fetchall()
        result["sources"] = [dict(source_content_id=r[0], source_file=r[1], note=r[2] or "") for r in rows]
        for source in result["sources"]:
            preview = None
            if {"visual_units", "derived_assets"} <= tables:
                # Same representative-frame policy as My Favorites. Historical
                # annotations contain only source IDs, not the favorited frame.
                preview = con.execute("""SELECT d.derived_path,
                    CASE WHEN v.time_position_ms >= 0 THEN v.time_position_ms
                         WHEN d.time_position_ms >= 0 THEN d.time_position_ms ELSE 0 END
                    FROM visual_units v JOIN derived_assets d USING(derived_id)
                    WHERE v.source_content_id=?
                    ORDER BY COALESCE(v.near_black,0),v.time_position_ms,v.visual_unit_id LIMIT 1""",
                    (source["source_content_id"],)).fetchone()
            elif {"stop03_5d_text_documents", "derived_assets"} <= tables:
                preview = con.execute("""SELECT a.derived_path,d.time_position_ms
                    FROM stop03_5d_text_documents d JOIN derived_assets a USING(derived_id)
                    WHERE d.source_content_id=? AND d.quality_status='PASS'
                    ORDER BY d.created_at DESC,d.time_position_ms,d.document_id LIMIT 1""",
                    (source["source_content_id"],)).fetchone()
            source.update(preview_path=str(preview[0] or "") if preview else "",
                          preview_time_ms=max(0, int(preview[1] or 0)) if preview else None,
                          preview_origin="representative_existing_frame" if preview else "unavailable")
        if not source_id:
            return result
        if source_id not in {r[0] for r in rows}:
            raise ValueError("该素材不在当前库收藏中；可能已取消收藏或切换了素材库。")
        if "stop03_5d_text_documents" not in tables:
            result["message"] = "这份库没有已有选片索引，不能凭空生成画面；收藏保留不变。"
            return result
        # Use the latest existing indexing run of this source, including sources
        # whose unchanged documents were reused by an incremental run.
        latest = con.execute("""SELECT embedding_run_id FROM stop03_5d_text_documents
            WHERE source_content_id=? GROUP BY embedding_run_id ORDER BY MAX(created_at) DESC LIMIT 1""", (source_id,)).fetchone()
        if latest is None:
            result["message"] = "该收藏尚无已有可选片画面；不会重新抽帧或分析。"
            return result
        params = (source_id, latest[0])
        result["total_frames"] = con.execute("""SELECT COUNT(*) FROM stop03_5d_text_documents
            WHERE source_content_id=? AND embedding_run_id=? AND quality_status='PASS'
            AND media_type IN ('image','video')""", params).fetchone()[0]
        rows = con.execute("""SELECT d.*,a.derived_path, NULL AS canonical_visual_unit_id,
            NULL AS source_extension,NULL AS source_size_bytes,NULL AS preview_width,NULL AS preview_height
            FROM stop03_5d_text_documents d LEFT JOIN derived_assets a USING(derived_id)
            WHERE d.source_content_id=? AND d.embedding_run_id=? AND d.quality_status='PASS'
              AND d.media_type IN ('image','video')
            ORDER BY d.time_position_ms,d.document_id LIMIT 9 OFFSET ?""", (*params, offset)).fetchall()
        for row in rows:
            raw = _candidate_from_row(row)
            candidate = _compact_candidate(raw, "favorite_manual")
            candidate.update(
                recommendation="人工收藏补选", gate_status="SOFT_GATE", gate_reason_codes=["USER_FAVORITE_MANUAL"],
                gate_reasons=["来自你的收藏，未自动判断与本句的适合度"], requires_source_review=True,
                match_reasons=["你收藏过这个原文件；这里按已索引时间点浏览"],
                fit_reason="这是人工补选来源，不是算法对本句的推荐结论。请查看原片再决定用途。",
                recommendation_reason="由用户从当前素材库收藏中手动补选，适合度由用户复核。",
                rank_reason="按原片时间点排列，不按本句匹配分排序。",
                provisional_in_ms=raw["start_ms"], provisional_out_ms=raw["end_ms"],
                duration_reason="以已有抽样点为中心的临时窗口；请播放原片调整并锁定剪点。")
            result["candidates"].append(candidate)
        end = offset + len(rows)
        result["next_offset"] = end if end < result["total_frames"] else None
    return result
