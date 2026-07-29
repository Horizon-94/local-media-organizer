#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse,csv,json,sqlite3,html
from pathlib import Path
from collections import Counter,defaultdict
SCRIPT_VERSION='stop03_2_v17_compare_contact_sheet_20260710_002000'
DEFAULT_DB=Path('/Users/yourname/Documents/AI-Local/media-archive-clean/media_archive.sqlite')
DEFAULT_V17=Path('/Users/yourname/Documents/AI-Local/test-output/stop03-2-candidate-queues-db-safe-v17_0_20260710_001500_full')
DEFAULT_V14=Path('/Users/yourname/Documents/AI-Local/test-output/stop03-2-candidate-queues-db-safe-v14_0_20260709_232500_full')
DEFAULT_OUT=Path('/Users/yourname/Documents/AI-Local/test-output/stop03-2-v17-compare-contact-sheet')
def read_csv(p):
    if not p.exists(): return []
    with p.open(newline='',encoding='utf-8') as f: return list(csv.DictReader(f))
def uri(p):
    if p and Path(p).exists(): return Path(p).as_uri()
    return ''
def tf(ms):
    try: s=max(0,int(ms)//1000)
    except Exception: return '--:--'
    return f'{s//60:02d}:{s%60:02d}'
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db',default=str(DEFAULT_DB)); ap.add_argument('--v17-out',default=str(DEFAULT_V17)); ap.add_argument('--v14-out',default=str(DEFAULT_V14)); ap.add_argument('--out-dir',default=str(DEFAULT_OUT)); a=ap.parse_args()
    db=Path(a.db); v17=Path(a.v17_out); v14=Path(a.v14_out); out=Path(a.out_dir); out.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(db)); c.row_factory=sqlite3.Row
    frames=[dict(r) for r in c.execute('''SELECT vu.visual_unit_id,vu.source_content_id,vu.visual_file,COALESCE(da.time_position_ms,vu.time_position_ms) AS time_position_ms,da.derived_path,sa.relative_path,sa.absolute_path FROM visual_units vu LEFT JOIN derived_assets da ON da.derived_id=vu.derived_id LEFT JOIN source_assets sa ON sa.source_content_id=vu.source_content_id WHERE sa.media_type='video' ORDER BY sa.relative_path,time_position_ms''')]
    labc={r['visual_unit_id']:r['n'] for r in c.execute('SELECT visual_unit_id,COUNT(*) AS n FROM visual_labels GROUP BY visual_unit_id')}
    v17q=read_csv(v17/'manifests/qwenvl_high_value_candidate_queue.csv'); v17o=read_csv(v17/'manifests/ocr_trigger_candidate_queue.csv'); v14q=read_csv(v14/'manifests/qwenvl_high_value_candidate_queue.csv')
    v17h={r['visual_unit_id'] for r in v17q if r.get('high_value_category') in ('video_coverage_keyframe','video_high_value_segment_candidate')}; v14h={r['visual_unit_id'] for r in v14q if r.get('high_value_category') in ('video_high_value_segment_candidate','video_coverage_keyframe')}; ocr={r['visual_unit_id'] for r in v17o}; yolo={k for k,v in labc.items() if v}
    by=defaultdict(list)
    for f in frames:
        try: f['time_position_ms']=int(f.get('time_position_ms') or -1)
        except Exception: f['time_position_ms']=-1
        f['source_relative_path']=f.get('relative_path') or f.get('absolute_path') or ''
        by[f['source_content_id']].append(f)
    css='''body{background:#111;color:#ddd;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:20px}.group{border:1px solid #333;border-radius:10px;margin:18px 0;padding:14px;background:#181818}.title{font-size:18px;font-weight:700}.meta{color:#aaa;margin:6px 0 12px}.strip{display:flex;gap:10px;overflow-x:auto}.card{min-width:150px;background:#222;border:3px solid #666;border-radius:8px;padding:6px}.card img{width:138px;height:86px;object-fit:cover;border-radius:4px;background:#333}.normal{border-color:#777}.yoloe{border-color:#21c875}.v14{border-color:#ff3b30}.v17{border-color:#2f80ff}.both{border-color:#b356ff}.badge{font-size:12px;font-weight:700;padding:2px 5px;border-radius:3px;margin-right:3px}.b-y{background:#21c875;color:#000}.b-v14{background:#ff3b30}.b-v17{background:#2f80ff}.b-both{background:#b356ff}.b-ocr{background:#ffd43b;color:#000}.ocrbar{height:6px;background:#ffd43b;border-radius:4px;margin-top:5px}.small{font-size:12px;color:#bbb}'''
    parts=[f'<!doctype html><meta charset="utf-8"><title>V17 vs V14</title><style>{css}</style><h1>V17 vs V14 视频关键帧对照</h1><p>灰=普通，绿=YOLOE，红=V14，蓝=V17，紫=重合，黄条=OCR</p>']
    summary=[]
    for i,(sid,fs) in enumerate(sorted(by.items(),key=lambda kv:(kv[1][0].get('source_relative_path',''),kv[0])),1):
        fs=sorted(fs,key=lambda x:(x.get('time_position_ms',-1),x.get('visual_unit_id',''))); path=fs[0].get('source_relative_path','') if fs else sid
        v14n=sum(f['visual_unit_id'] in v14h for f in fs); v17n=sum(f['visual_unit_id'] in v17h for f in fs); bothn=sum(f['visual_unit_id'] in v14h and f['visual_unit_id'] in v17h for f in fs); ocrn=sum(f['visual_unit_id'] in ocr for f in fs); yn=sum(f['visual_unit_id'] in yolo for f in fs)
        ts=[f.get('time_position_ms',-1) for f in fs if f.get('time_position_ms',-1)>=0]; dur=(max(ts)-min(ts))/1000 if ts else 0
        summary.append({'idx':i,'source_content_id':sid,'path':path,'frames':len(fs),'duration_s':round(dur,3),'v14':v14n,'v17':v17n,'both':bothn,'ocr':ocrn,'yoloe':yn})
        parts.append(f'<div class="group"><div class="title">{i:03d}. {html.escape(path)}</div><div class="meta">source_content_id={html.escape(sid)} | frames={len(fs)} | duration≈{dur:.1f}s | V14={v14n} | V17={v17n} | both={bothn} | OCR={ocrn} | YOLOE={yn}</div><div class="strip">')
        for f in fs:
            vu=f['visual_unit_id']; cls='normal'; badges=[]
            if vu in v14h and vu in v17h: cls='both'; badges.append('<span class="badge b-both">BOTH</span>')
            elif vu in v14h: cls='v14'; badges.append('<span class="badge b-v14">V14</span>')
            elif vu in v17h: cls='v17'; badges.append('<span class="badge b-v17">V17</span>')
            elif vu in yolo: cls='yoloe'; badges.append('<span class="badge b-y">YOLOE</span>')
            if vu in ocr: badges.append('<span class="badge b-ocr">OCR</span>')
            src=uri(f.get('visual_file') or f.get('derived_path') or '')
            img=f'<img src="{src}">' if src else '<div style="width:138px;height:86px;background:#333"></div>'
            parts.append(f'<div class="card {cls}">{img}<div>{"".join(badges)}</div><div class="small">{tf(f.get("time_position_ms"))} | labels={labc.get(vu,0)}</div>{"<div class=ocrbar></div>" if vu in ocr else ""}</div>')
        parts.append('</div></div>')
    htmlp=out/'v17_vs_v14_video_frame_contact_sheet.html'; htmlp.write_text('\n'.join(parts),encoding='utf-8')
    csvp=out/'v17_vs_v14_video_group_summary.csv'
    with csvp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(summary[0].keys()) if summary else []); w.writeheader(); w.writerows(summary)
    res={'script_version':SCRIPT_VERSION,'db':str(db),'v17_out':str(v17),'v14_out':str(v14),'video_group_count':len(by),'video_frame_count':len(frames),'v14_video_high_count':len(v14h),'v17_video_high_count':len(v17h),'overlap_count':len(v14h&v17h),'ocr_video_frame_count':len(ocr),'output_html':str(htmlp),'output_group_summary_csv':str(csvp)}
    print(json.dumps(res,ensure_ascii=False,indent=2)); print('HTML:',htmlp)
if __name__=='__main__': main()
