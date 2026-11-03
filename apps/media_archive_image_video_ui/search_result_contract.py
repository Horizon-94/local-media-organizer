"""Presentation contract over already eligible search hits; never gates relevance."""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePath
from typing import Any, Iterable

CONTRACT = "media_archive_source_search_v2"
PAGE_SIZE = 30
# Existing native search uses a 5-second temporal window. This is proximity,
# not shot detection; chained adjacent hits may form a longer continuous span.
TEMPORAL_GAP_MS = 5000
FILTER_CONTRACT = {
    "image": "SUPPORTED", "video": "SUPPORTED", "person": "SUPPORTED_PRESENCE_AND_EXISTING_PERSON_ROUTE",
    "ocr": "SUPPORTED_PRESENCE", "transcript": "SUPPORTED_EXPLICIT_AUDIO_ROUTE",
    "folder": "SUPPORTED_PATH_PREFIX", "time": "SUPPORTED_SOURCE_MTIME_RANGE",
    "tags": "NOT_SUPPORTED_AS_QUERY_FILTER", "filename": "NOT_SUPPORTED_AS_QUERY_FILTER",
    "camera": "NOT_SUPPORTED", "rating": "NOT_SUPPORTED", "duration": "NOT_SUPPORTED",
}



def identity(row: dict[str, Any]) -> str:
    return str(row.get("source_content_id") or row.get("source_id") or "")


def score(row: dict[str, Any]) -> float:
    return float(row.get("relevance_score", row.get("score", row.get("hybrid_score", 0))) or 0)


def reasons(row: dict[str, Any]) -> list[dict[str, Any]]:
    if "match_reasons" in row:
        return list(row["match_reasons"])
    result = []
    for label in row.get("matched_object_labels") or []:
        result.append({"type": "OBJECT", "authority": "YOLOE", "label": label.get("label"),
                       "confidence": label.get("confidence"), "bbox": label.get("bbox")})
    if "strong_visual_semantic" in (row.get("relevance_reasons") or []):
        result.append({"type": "SEMANTIC_VISUAL", "authority": "OpenCLIP", "score": row.get("openclip_cosine")})
    if row.get("audio_transcript_match"):
        result.append({"type": "TRANSCRIPT", "authority": "Whisper", "text": row.get("text_preview")})
    elif row.get("text_evidence_present") or row.get("text_exact_match"):
        channels = row.get("text_evidence_channels") or ["TEXT_DOCUMENT"]
        for channel in channels:
            result.append({"type": channel, "authority": "current_document", "text": row.get("text_preview"),
                           "score": row.get("text_semantic_score")})
    # Unknown document provenance remains TEXT_DOCUMENT; never claim YOLOE.
    return result


def union_reasons(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {json.dumps(reason, sort_keys=True, ensure_ascii=False): reason
              for row in rows for reason in reasons(row)}
    return [unique[key] for key in sorted(unique)]


def representative_key(row: dict[str, Any]) -> tuple:
    return (-score(row), -len(reasons(row)), -float(row.get("hybrid_score") or 0),
            int(row.get("time_position_ms") or 0), str(row.get("visual_unit_id") or row.get("result_id") or ""))


def frame(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: row.get(key) for key in ("result_id", "visual_unit_id", "derived_id", "preview_path", "time_position_ms")}
    result["preview_path"] = row.get("preview_path") or row.get("derived_path")
    return result


def _legacy_source_results(rows: Iterable[dict[str, Any]], gap_ms: int = TEMPORAL_GAP_MS) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for original in rows:
        # Visibility remains authoritative upstream; explicit invisible inputs
        # cannot be resurrected by presentation or progressive merging.
        if original.get("visible") is False or original.get("current") is False:
            continue
        sid = identity(original)
        if not sid:
            continue
        hit_id = str(original.get("result_id") or original.get("visual_unit_id") or original.get("document_id") or "")
        grouped.setdefault(sid, {})[hit_id] = dict(original)
    results = []
    for sid, hit_map in grouped.items():
        hits = list(hit_map.values())
        best = min(hits, key=representative_key)
        item = dict(best)
        clusters = []
        if str(best.get("media_type", "")).lower() == "video":
            batches: list[list[dict[str, Any]]] = []
            for hit in sorted(hits, key=lambda h: (int(h.get("time_position_ms") or 0), representative_key(h))):
                if not batches or int(hit.get("time_position_ms") or 0) - int(batches[-1][-1].get("time_position_ms") or 0) > gap_ms:
                    batches.append([])
                batches[-1].append(hit)
            for batch in batches:
                rep = min(batch, key=representative_key)
                start = int(batch[0].get("time_position_ms") or 0)
                end = int(batch[-1].get("time_position_ms") or 0)
                cid = hashlib.sha256(f"{sid}:{start}:{end}".encode()).hexdigest()[:24]
                clusters.append({"cluster_id": cid, "start_time_ms": start, "end_time_ms": end,
                                 "representative_time_ms": int(rep.get("time_position_ms") or 0),
                                 "representative_frame": frame(rep), "match_count": len(batch),
                                 "top_score": score(rep), "match_reasons": union_reasons(batch)})
        top = next((c for c in clusters if c["start_time_ms"] <= int(best.get("time_position_ms") or 0) <= c["end_time_ms"]), None)
        path = str(best.get("source_path") or best.get("source_relative_path") or "")
        item.update({"source_id": sid, "source_content_id": sid,
                     "source_type": str(best.get("media_type") or "image").upper(),
                     "original_path": path, "display_name": PurePath(path).name,
                     "representative_thumbnail": best.get("preview_path") or best.get("derived_path"),
                     "top_match": top or frame(best), "match_clusters": clusters,
                     "other_match_count": max(0, len(clusters) - 1),
                     "other_match_clusters": [c for c in clusters if c is not top],
                     "source_match_count": len(hits), "match_reasons": union_reasons(hits),
                     "final_score": score(best), "search_result_contract": CONTRACT,
                     "source_match_time_positions_ms": sorted({int(h.get("time_position_ms") or 0) for h in hits}),
                     "source_match_time_span_start_ms": clusters[0]["start_time_ms"] if clusters else None,
                     "source_match_time_span_end_ms": clusters[-1]["end_time_ms"] if clusters else None})
        results.append(item)
    return sorted(results, key=lambda item: (-item["final_score"], item["source_id"]))


def page(rows: list[dict[str, Any]], offset: int = 0) -> dict[str, Any]:
    offset = max(0, int(offset))
    return {"result_items": rows[offset:offset + PAGE_SIZE], "result_total_count": len(rows),
            "result_offset": offset, "result_limit": PAGE_SIZE,
            "next_result_offset": offset + PAGE_SIZE if offset + PAGE_SIZE < len(rows) else None}


class ProgressiveResults:
    """Source upserts; same-query clients replace by source_id, never append hits."""
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}
        self.generation = 0

    def snapshot(self, rows, *, complete=False, scanned_count=0, total_count=None):
        incoming = {identity(row): row for row in rows}
        if complete:
            self.rows = incoming
        else:
            self.rows.update(incoming)
        ordered = sorted(self.rows.values(), key=lambda row: (-round(float(row["final_score"]), 2), identity(row)))
        if complete:
            ordered = sorted(self.rows.values(), key=lambda row: (-float(row["final_score"]), identity(row)))
        self.generation += 1
        return {"contract": CONTRACT, "search_state": "COMPLETE" if complete else "SEARCHING",
                "generation": self.generation, "update_mode": "UPSERT_BY_SOURCE",
                "ordering": "FINAL" if complete else "PROVISIONAL_SCORE_BUCKET_0_01",
                "scanned_count": scanned_count, "total_count": total_count,
                "current_source_result_count": len(self.rows), **page(ordered)}


def independent_family(hit):
    category=hit.get('evidence_category','other_clue')
    return category if category in {'visual_description','visual_semantic','dialogue','ocr'} else None

def overlap(a,b):
    if (identity(a),a.get('source_revision'))!=(identity(b),b.get('source_revision')):return False
    sa=int(a.get('time_position_ms') or 0);sb=int(b.get('time_position_ms') or 0)
    ea=max(sa,int(a.get('result_end_ms') or sa));eb=max(sb,int(b.get('result_end_ms') or sb))
    if a.get('evidence_category') in {'visual_description','visual_semantic'}:ea=sa
    if b.get('evidence_category') in {'visual_description','visual_semantic'}:eb=sb
    return max(sa,sb)<=min(ea,eb)

def fuse_moments(hits):
    # Each anchor is independently scored. No chain of nearby points, cross-source
    # union, or accumulation simply because a long video has more occurrences.
    for anchor in hits:
        base=score(anchor);family=independent_family(anchor);support={}
        if family:
            for hit in hits:
                other=independent_family(hit)
                if other and other!=family and overlap(anchor,hit):
                    if other not in support or score(hit)>score(support[other]):support[other]=hit
        values=sorted((score(h) for h in support.values()),reverse=True)
        anchor['_fusion_score']=base+min(base*.375,sum(min(base,v)*w for v,w in zip(values,(.25,.125))))
        anchor['fusion_evidence']=[{'category':h.get('evidence_category'),'document_id':h.get('result_id'),
            'source_revision':h.get('source_revision'),'start_ms':h.get('time_position_ms'),'end_ms':h.get('result_end_ms')}
            for h in support.values()]
    for hit in hits:
        hit['base_score']=score(hit);hit['score']=hit.pop('_fusion_score')
        hit['relevance_score']=hit['score']

def evidence_summary(hits):
    labels={'visual_description':'画面描述','visual_semantic':'视觉语义','dialogue':'对白','ocr':'OCR文字',
            'expanded_visual_clue':'扩展关键词/同义线索','neighbor_clue':'邻帧线索','filename':'文件名'}
    first={}
    for h in sorted(hits,key=lambda h:int(h.get('time_position_ms') or 0)):
        first.setdefault(h.get('evidence_category'),int(h.get('time_position_ms') or 0))
    return '；'.join(f"{labels.get(k,'其他线索')} {t//60000:02}:{t//1000%60:02}.{t%1000:03}" for k,t in first.items())

def source_results(rows, gap_ms=TEMPORAL_GAP_MS):
    rows=list(rows)
    if not any('search_intent' in r for r in rows):
        return _legacy_source_results(rows,gap_ms)
    grouped={}
    for original in rows:
        if original.get('visible') is False or original.get('current') is False:continue
        sid=identity(original)
        if not sid:continue
        row=dict(original);category=row.get('evidence_category','other_clue')
        family=category
        moment=(row.get('source_revision'),family,row.get('visual_unit_id') if family in {'visual_semantic','visual_description'} else None,
                row.get('time_position_ms'),row.get('result_end_ms') if family not in {'visual_semantic','visual_description'} else None)
        hits=grouped.setdefault((sid,row.get('source_revision')),{})
        previous=hits.get(moment)
        if previous is None:hits[moment]=row
        else:
            # Independent channel duplicates do not add to relevance. Preserve
            # all same-moment evidence in diagnostics, never inherit another time.
            best=min((previous,row),key=representative_key)
            merged=dict(best);merged['match_reasons']=union_reasons((previous,row))
            merged['evidence_document_ids']=sorted(set(previous.get('evidence_document_ids',[previous.get('result_id')])+[row.get('result_id')]))
            hits[moment]=merged
    results=[]
    for (sid,revision),moments in grouped.items():
        hits=list(moments.values());fuse_moments(hits);best=min(hits,key=representative_key);item=dict(best);clusters=[];top=None
        for hit in sorted(hits,key=lambda r:(int(r.get('time_position_ms') or 0),representative_key(r))):
            start=int(hit.get('time_position_ms') or 0)
            # A visual point is not a duration or a promised ten-second shot.
            end=start if hit.get('evidence_category') in {'visual_semantic','visual_description'} else int(hit.get('result_end_ms') or start)
            cid=hashlib.sha256(json.dumps((sid,hit.get('source_revision'),hit.get('evidence_category'),hit.get('visual_unit_id'),start,end)).encode()).hexdigest()[:24]
            cluster={'cluster_id':cid,'start_time_ms':start,'end_time_ms':end,'representative_time_ms':start,
                'representative_frame':frame(hit),'match_count':1,'top_score':score(hit),
                'match_reasons':reasons(hit),'evidence_category':hit.get('evidence_category'),'evidence_text':hit.get('text_preview','')}
            clusters.append(cluster)
            if hit is best:top=cluster
        path=str(best.get('source_path') or best.get('source_relative_path') or '')
        item.update(source_id=sid,source_content_id=sid,source_type=str(best.get('media_type') or 'image').upper(),
            original_path=path,display_name=PurePath(path).name,representative_thumbnail=best.get('preview_path'),
            top_match=top or frame(best),match_clusters=clusters,other_match_count=max(0,len(clusters)-1),
            other_match_clusters=[c for c in clusters if c is not top],source_match_count=len(hits),
            match_reasons=reasons(best),final_score=score(best),search_result_contract=CONTRACT,
            source_match_time_positions_ms=sorted({int(h.get('time_position_ms') or 0) for h in hits}),
            source_match_time_span_start_ms=None,source_match_time_span_end_ms=None,
            evidence_summary=evidence_summary(hits),fusion_policy='same_text_max_independent_overlap_capped_v1')
        results.append(item)
    return sorted(results,key=lambda r:(-r['final_score'],r['source_id']))
