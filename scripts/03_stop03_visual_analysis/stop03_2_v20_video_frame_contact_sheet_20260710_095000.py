#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, csv, html, json, sqlite3
from collections import defaultdict
from pathlib import Path

SCRIPT_VERSION='stop03_2_v20_contact_sheet_20260710_095000'
DEFAULT_DB=Path('/Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite')
DEFAULT_V20_OUT=Path('/Users/yourname/Documents/AI-Local/test-output/stop03-2-candidate-queues-db-safe-v20_0_20260710_094500_full')
DEFAULT_OUT_DIR=Path('/Users/yourname/Documents/AI-Local/test-output/stop03-2-v20-video-frame-contact-sheet')

def conn(db):
    c=sqlite3.connect(str(db)); c.row_factory=sqlite3.Row; return c

def read_csv(path):
    p=Path(path)
    if not p.exists(): return []
    with p.open(newline='',encoding='utf-8') as f: return list(csv.DictReader(f))

def timefmt(ms):
    try: s=max(0,int(ms)//1000)
    except Exception: return '--:--'
    return f'{s//60:02d}:{s%60:02d}'

def uri(path):
    if not path: return ''
    p=Path(path)
    return p.as_uri() if p.exists() else ''

def load_frames(c):
    rows=c.execute("""SELECT vu.visual_unit_id,vu.source_content_id,vu.derived_id,vu.visual_file,COALESCE(da.time_position_ms,vu.time_position_ms) AS time_position_ms,da.derived_path,sa.relative_path,sa.absolute_path,sa.media_type FROM visual_units vu LEFT JOIN derived_assets da ON da.derived_id=vu.derived_id LEFT JOIN source_assets sa ON sa.source_content_id=vu.source_content_id WHERE sa.media_type='video' ORDER BY sa.relative_path,time_position_ms""").fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        try: d['time_position_ms']=int(d.get('time_position_ms') or -1)
        except Exception: d['time_position_ms']=-1
        d['source_relative_path']=d.get('relative_path') or d.get('absolute_path') or ''
        out.append(d)
    return out

def label_counts(c):
    return {r['visual_unit_id']:r['n'] for r in c.execute('SELECT visual_unit_id,COUNT(*) AS n FROM visual_labels GROUP BY visual_unit_id')}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--db',default=str(DEFAULT_DB))
    ap.add_argument('--v20-out',default=str(DEFAULT_V20_OUT))
    ap.add_argument('--out-dir',default=str(DEFAULT_OUT_DIR))
    args=ap.parse_args()
    db=Path(args.db); v20=Path(args.v20_out); outdir=Path(args.out_dir); outdir.mkdir(parents=True,exist_ok=True)
    c=conn(db); frames=load_frames(c); labs=label_counts(c)
    q=read_csv(v20/'manifests/qwenvl_high_value_candidate_queue.csv')
    o=read_csv(v20/'manifests/ocr_trigger_candidate_queue.csv')
    cats={r.get('visual_unit_id'):r.get('high_value_category') for r in q}
    scores={r.get('visual_unit_id'):r.get('candidate_score') for r in q}
    ocr={r.get('visual_unit_id') for r in o}
    yoloe={vu for vu,n in labs.items() if n}
    by=defaultdict(list)
    for f in frames: by[f['source_content_id']].append(f)
    css="""body{background:#111;color:#ddd;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:20px}.legend span{display:inline-block;margin-right:12px;padding:4px 8px;border-radius:4px}.group{border:1px solid #333;border-radius:10px;margin:18px 0;padding:14px;background:#181818}.title{font-size:18px;font-weight:700;margin-bottom:6px}.meta{color:#aaa;margin-bottom:12px}.strip{display:flex;gap:10px;overflow-x:auto;padding-bottom:8px}.card{min-width:150px;max-width:150px;background:#222;border:3px solid #666;border-radius:8px;padding:6px}.card img{width:138px;height:86px;object-fit:cover;border-radius:4px;display:block;background:#333}.normal{border-color:#777}.yoloe{border-color:#21c875}.coverage{border-color:#2f80ff}.supp{border-color:#ff3b30}.overlap{border-color:#b356ff}.ocrbar{height:6px;background:#ffd43b;border-radius:4px;margin-top:5px}.badge{font-size:12px;font-weight:700;padding:2px 5px;border-radius:3px;margin-right:3px}.b-y{background:#21c875;color:#000}.b-c{background:#2f80ff}.b-s{background:#ff3b30}.b-o{background:#b356ff}.b-ocr{background:#ffd43b;color:#000}.small{font-size:12px;color:#bbb;line-height:1.4}"""
    parts=["<!doctype html><meta charset='utf-8'><title>V20 Video Frame Contact Sheet</title><style>"+css+"</style>",'<h1>V20：V17 覆盖主层 + V14 高信号补充</h1>']
    parts.append("<div class='legend'><span style='border:3px solid #777'>灰=普通</span><span style='border:3px solid #21c875'>绿=YOLOE</span><span style='border:3px solid #2f80ff'>蓝=V17覆盖</span><span style='border:3px solid #ff3b30'>红=V14补充</span><span style='border:3px solid #b356ff'>紫=覆盖+信号重合</span><span style='background:#ffd43b;color:#000'>黄条=OCR</span></div>")
    summary=[]
    for idx,(sid,fs) in enumerate(sorted(by.items(),key=lambda kv:(kv[1][0].get('source_relative_path',''),kv[0])),1):
        fs=sorted(fs,key=lambda x:(x.get('time_position_ms',-1),x.get('visual_unit_id','')))
        path=fs[0].get('source_relative_path','') if fs else sid
        cnt={k:0 for k in ['coverage','supp','overlap','ocr','yoloe']}
        for f in fs:
            cat=cats.get(f['visual_unit_id'],'')
            if cat=='video_coverage_keyframe': cnt['coverage']+=1
            elif cat=='video_high_signal_supplement': cnt['supp']+=1
            elif cat=='video_coverage_high_signal_overlap': cnt['overlap']+=1
            if f['visual_unit_id'] in ocr: cnt['ocr']+=1
            if f['visual_unit_id'] in yoloe: cnt['yoloe']+=1
        ts=[f.get('time_position_ms',-1) for f in fs if f.get('time_position_ms',-1)>=0]
        dur=(max(ts)-min(ts))/1000 if ts else 0
        summary.append({'idx':idx,'source_content_id':sid,'path':path,'frames':len(fs),'duration_s':round(dur,3),**cnt})
        parts.append("<div class='group'>")
        parts.append(f"<div class='title'>{idx:03d}. {html.escape(path)}</div>")
        parts.append(f"<div class='meta'>source_content_id={html.escape(sid)} | frames={len(fs)} | duration≈{dur:.1f}s | coverage={cnt['coverage']} | supplement={cnt['supp']} | overlap={cnt['overlap']} | OCR={cnt['ocr']} | YOLOE={cnt['yoloe']}</div>")
        parts.append("<div class='strip'>")
        for f in fs:
            vu=f['visual_unit_id']; cat=cats.get(vu,''); cls='normal'; badges=[]
            if cat=='video_coverage_high_signal_overlap': cls='overlap'; badges.append("<span class='badge b-o'>BOTH</span>")
            elif cat=='video_high_signal_supplement': cls='supp'; badges.append("<span class='badge b-s'>SUPP</span>")
            elif cat=='video_coverage_keyframe': cls='coverage'; badges.append("<span class='badge b-c'>COV</span>")
            elif vu in yoloe: cls='yoloe'; badges.append("<span class='badge b-y'>YOLOE</span>")
            if vu in ocr: badges.append("<span class='badge b-ocr'>OCR</span>")
            src=uri(f.get('visual_file') or f.get('derived_path') or '')
            img=f"<img src='{src}'>" if src else "<div style='width:138px;height:86px;background:#333'></div>"
            sc=f"Q={scores.get(vu)}" if vu in cats else ''
            parts.append(f"<div class='card {cls}'>{img}<div>{''.join(badges)}</div><div class='small'>{timefmt(f.get('time_position_ms'))} | labels={labs.get(vu,0)}<br>{html.escape(sc)}</div>{'<div class=ocrbar></div>' if vu in ocr else ''}</div>")
        parts.append('</div></div>')
    html_path=outdir/'v20_video_frame_contact_sheet.html'; html_path.write_text('\n'.join(parts),encoding='utf-8')
    csv_path=outdir/'v20_video_group_summary.csv'
    with csv_path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(summary[0].keys()) if summary else []); w.writeheader(); w.writerows(summary)
    result={'script_version':SCRIPT_VERSION,'db':str(db),'v20_out':str(v20),'video_group_count':len(by),'video_frame_count':len(frames),'output_html':str(html_path),'output_group_summary_csv':str(csv_path)}
    print(json.dumps(result,ensure_ascii=False,indent=2)); print('HTML:',html_path)
if __name__=='__main__': main()
