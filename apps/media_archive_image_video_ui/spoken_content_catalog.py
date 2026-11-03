"""Read-only paginated transcript catalogue over current, published V2 evidence.

Classification is a rule-based hint, never speaker attribution or an AI summary.
Only metadata aggregates are computed across the library; text detail is fetched
for sources on the requested page. No original-media probe or model invocation.
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from .video_content_timeline import _open_readonly

CONTRACT = 'media_archive_spoken_catalog_v1'
PAGE_SIZE = 20
KINDS = ('all', 'likely_interview', 'long_spoken_content')
QUESTION = re.compile(r'[?？]|你(?:认为|觉得|想|会|是|在|对|更|能|希望)|有没有|为什么|为何|什么是|是什么|怎么|怎样|如何|哪一|哪个|多久|多少|是否|能否|想知道|请问|\b(?:why|how|what|where|when|who)\b', re.I)

def is_question(text):
    return bool(QUESTION.search(str(text or '')))

def classify_metrics(span_ms, covered_ms, segments, characters, qa_count, longest_run_ms):
    """Span is context; union coverage and continuous runs prevent sparse false positives."""
    substantial = covered_ms >= 90000 and segments >= 10 and characters >= 180 and longest_run_ms >= 60000
    if substantial and span_ms >= 120000 and qa_count:
        return 'likely_interview'
    return 'long_spoken_content' if substantial else 'fragmented_speech'

# Window MAX unions overlapping intervals; gaps >15s start a new speech run.
# LEAD is within one source/revision, so a question cannot borrow another video.
BASE = '''WITH valid AS MATERIALIZED (
 SELECT d.document_id,d.source_id,d.source_revision,length(trim(d.text)) characters,is_question(d.text) question,
        max(0,coalesce(d.start_ms,0)) start_ms,
        max(0,coalesce(d.start_ms,0),coalesce(d.end_ms,d.start_ms,0)) end_ms
 FROM v2_documents d
 JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref AND j.status='DONE'
   AND j.source_id=d.source_id AND j.source_revision=d.source_revision
 JOIN v2_active_sources a ON a.source_id=d.source_id AND a.source_revision=d.source_revision
 JOIN release_search_heads h ON h.source_id=d.source_id AND h.revision=d.source_revision AND h.visible=1
 JOIN release_registry r ON r.source_id=h.source_id AND r.revision=h.revision AND r.present=1 AND r.committed=1
 JOIN v2_source_metadata m ON m.source_id=d.source_id AND m.source_revision=d.source_revision
 WHERE d.document_type='whisper' AND length(trim(d.text))>0
   AND lower(coalesce(json_extract(m.metadata,'$.media_type'),json_extract(m.metadata,'$.parent_media_kind'),'video'))='video'
   AND d.source_revision NOT LIKE 'aborted:%' AND d.source_revision NOT LIKE 'fenced:%'
   AND (?=0 OR EXISTS(SELECT 1 FROM json_each(?) p WHERE p.value=d.source_id))
) , question_groups AS MATERIALIZED (
 SELECT *,sum(question) OVER(PARTITION BY source_id,source_revision ORDER BY start_ms,end_ms,document_id) question_group
 FROM valid
), ordered AS MATERIALIZED (
 SELECT *,sum(CASE WHEN question THEN 0 ELSE characters END)
 OVER(PARTITION BY source_id,source_revision,question_group ORDER BY start_ms RANGE BETWEEN CURRENT ROW AND 90000 FOLLOWING) answer_characters,
 max(end_ms) OVER(PARTITION BY source_id,source_revision ORDER BY start_ms,end_ms,document_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prior_end,
 lead(start_ms) OVER(PARTITION BY source_id,source_revision ORDER BY start_ms,end_ms,document_id) answer_start
 FROM question_groups
), marked AS MATERIALIZED (
 SELECT *,max(0,end_ms-max(start_ms,coalesce(prior_end,start_ms))) covered,
 sum(CASE WHEN prior_end IS NULL OR start_ms-prior_end>15000 THEN 1 ELSE 0 END)
 OVER(PARTITION BY source_id,source_revision ORDER BY start_ms,end_ms,document_id) run_id
 FROM ordered
), runs AS MATERIALIZED (
 SELECT source_id,source_revision,run_id,sum(covered) run_ms FROM marked GROUP BY source_id,source_revision,run_id
), metrics AS MATERIALIZED (
 SELECT source_id,source_revision,min(start_ms) start_ms,max(end_ms) end_ms,
 count(*) segment_count,sum(characters) character_count,sum(covered) covered_ms,
 max(max(0,start_ms-coalesce(prior_end,start_ms))) longest_gap_ms,
 sum(CASE WHEN question AND answer_characters>=24 AND answer_start-end_ms<=30000 THEN 1 ELSE 0 END) qa_count,
 (SELECT max(run_ms) FROM runs r WHERE r.source_id=marked.source_id AND r.source_revision=marked.source_revision) longest_run_ms
 FROM marked GROUP BY source_id,source_revision
), classified AS MATERIALIZED (
 SELECT *,classify_spoken(end_ms-start_ms,covered_ms,segment_count,character_count,qa_count,longest_run_ms) content_kind FROM metrics
) '''

def catalogue(database, *, content_kind='all', offset=0, source_ids=None, library_name=''):
    if content_kind not in KINDS:raise ValueError('spoken_content_kind_invalid')
    offset=max(0,int(offset)); params=(int(source_ids is not None),json.dumps(sorted(set(source_ids or []))))
    with _open_readonly(Path(database)) as c:
        tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if not {'release_search_heads','release_registry','v2_documents','v2_source_metadata'} <= tables:
            return {'status':'UNAVAILABLE','contract':CONTRACT,'items':[],'total':0,'offset':offset,'limit':PAGE_SIZE,'next_offset':None,'message':'当前素材库没有可用的已发布转写目录'}
        c.create_function('is_question',1,is_question,deterministic=True)
        c.create_function('classify_spoken',6,classify_metrics,deterministic=True)
        where=" WHERE (?='all' OR content_kind=?)"
        total=c.execute(BASE+'SELECT count(*) FROM classified'+where,params+(content_kind,content_kind)).fetchone()[0]
        rows=c.execute(BASE+'''SELECT x.*,m.path FROM classified x JOIN v2_source_metadata m
          ON m.source_id=x.source_id AND m.source_revision=x.source_revision'''+where+
          ' ORDER BY x.source_id,x.source_revision LIMIT ? OFFSET ?',params+(content_kind,content_kind,PAGE_SIZE,offset)).fetchall()
        items=[]
        for row in rows:
            item=dict(row);sid=item['source_id'];rev=item['source_revision']
            # Text is loaded only for this page, from the same fixed snapshot.
            detail=c.execute(BASE+'''SELECT o.document_id,o.start_ms,o.end_ms,d.text,o.answer_start,o.answer_characters FROM ordered o
                JOIN v2_documents d ON d.document_id=o.document_id
                WHERE o.source_id=? AND o.source_revision=? ORDER BY o.start_ms,o.end_ms,o.document_id''',(1,json.dumps([sid]),sid,rev)).fetchall()
            qa=[]
            for i,d in enumerate(detail):
                if not is_question(d['text']) or d['answer_characters']<24 or d['answer_start'] is None or d['answer_start']-d['end_ms']>30000:continue
                following=[];characters=0
                for after in detail[i+1:]:
                    if is_question(after['text']) or after['start_ms']-d['start_ms']>90000 or characters>=360:break
                    following.append(after);characters+=len(after['text'].strip())
                if characters<24:continue
                qa.append({'document_id':d['document_id'],'start_ms':d['start_ms'],'question':d['text'],
                    'answer':'，'.join(a['text'] for a in following)[:500],
                    'answer_start_ms':following[0]['start_ms'],'answer_end_ms':following[-1]['end_ms'],
                    'basis':'疑问表达后30秒内有后续转写，90秒窗口内、下一疑问前累计至少24字；不证明不同说话人'})
                if len(qa)>=3:break
            item.update(display_name=Path(item.pop('path')).name,library_name=library_name,
                excerpt=str(detail[0]['text'])[:240] if detail else '',qa_clues=qa,
                classification_reason=f"{item['segment_count']}段 · {item['character_count']}字 · 转写覆盖{item['covered_ms']/1000:.1f}秒 · 最长连续组{item['longest_run_ms']/1000:.1f}秒 · 最大间隔{item['longest_gap_ms']/1000:.1f}秒",
                coverage_note='仅展示已有转写；未证明整段视频完整转写。出镜人物不等于说话人。',
                content_label={'likely_interview':'疑似采访 / 问答','long_spoken_content':'长口播 / 长对话','fragmented_speech':'零散语音'}[item['content_kind']])
            items.append(item)
        return {'status':'PASS','contract':CONTRACT,'items':items,'total':total,'offset':offset,'limit':PAGE_SIZE,
            'next_offset':offset+PAGE_SIZE if offset+PAGE_SIZE<total else None,
            'message':'基于已有转写的规则整理，不含说话人识别或生成式摘要。','database_write':False,'model_run':False}
