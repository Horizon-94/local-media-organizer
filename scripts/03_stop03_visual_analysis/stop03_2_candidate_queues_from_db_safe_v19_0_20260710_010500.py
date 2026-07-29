#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import argparse, csv, hashlib, json, os, platform, sqlite3, sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_VERSION='stop03_2_candidate_queues_from_db_safe_v19_0_20260710_010500'
POLICY_VERSION='stop03_2_generic_high_value_rules_v19_v17_base_v14_supplement_final_dedup_selective_ocr_20260710'
PROJECT_ROOT=Path('/Users/yourname/Documents/AI-Local/media-archive-clean')
TEST_OUTPUT_ROOT=Path('/Users/yourname/Documents/AI-Local/test-output')
DEFAULT_DB=PROJECT_ROOT/'media_archive.sqlite'
DEFAULT_OUT=TEST_OUTPUT_ROOT/'stop03-2-candidate-queues-db-safe-v19_0_20260710_010500_full'
DEFAULT_V14_OUT=TEST_OUTPUT_ROOT/'stop03-2-candidate-queues-db-safe-v14_0_20260709_232500_full'
EXPECTED_PY=Path('/Users/yourname/Documents/AI-Local/envs/media-archive-v06-visual/bin/python')
SCREEN_KEYS=['rpreplay','screenrecording','screen_recording','screen-recording','screen recording','record screen','recorded screen','录屏','屏幕录制','截屏','截图','screenshot']
TEXT_LABELS={'screen','screen recording','phone screen','subtitle','text','sign','billboard','poster','book','document','paper','whiteboard','blackboard','presentation slide','logo','ticket','phone','laptop','tablet'}
WEIGHTS={'person':2.8,'face':2.4,'car':1.8,'bus':1.9,'truck':2.0,'tractor':2.0,'harvester':2.2,'camera':1.8,'phone':1.8,'dog':1.8,'cat':1.8,'sign':2.2,'screen':2.2,'whiteboard':2.0,'blackboard':2.0,'book':1.8,'document':1.8,'billboard':2.0,'crop':1.7,'farmland':1.7,'field':1.5,'tree':1.2,'lake':1.4,'road':1.3,'building':1.4}

def now(): return datetime.now().isoformat(timespec='seconds')
def sha(s): return hashlib.sha1(s.encode('utf-8','ignore')).hexdigest()
def offline():
    d={'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','HF_DATASETS_OFFLINE':'1','ULTRALYTICS_OFFLINE':'1','NO_ALBUMENTATIONS_UPDATE':'1','YOLO_CONFIG_DIR':str(TEST_OUTPUT_ROOT/'ultralytics-offline-config')}
    for k,v in d.items(): os.environ[k]=v
    return d

def conn(db):
    c=sqlite3.connect(str(db)); c.row_factory=sqlite3.Row; return c

def table_cols(c,t):
    try: return [r['name'] for r in c.execute(f'PRAGMA table_info({t})')]
    except Exception: return []

def is_screen(path):
    h=(path or '').lower(); return any(k.lower() in h for k in SCREEN_KEYS)


def parse_bbox(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8', errors='ignore')
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    vals = None
    if isinstance(obj, dict):
        if 'bbox' in obj:
            vals = obj.get('bbox')
        elif all(k in obj for k in ('x1','y1','x2','y2')):
            vals = [obj['x1'], obj['y1'], obj['x2'], obj['y2']]
        elif all(k in obj for k in ('x','y','w','h')):
            x,y,w,h = obj['x'], obj['y'], obj['w'], obj['h']
            vals = [x, y, x+w, y+h]
    elif isinstance(obj, list):
        vals = obj
    if not isinstance(vals, list) or len(vals) < 4:
        return None
    try:
        x1,y1,x2,y2 = [float(x) for x in vals[:4]]
    except Exception:
        return None
    # Only trust normalized bbox. If absolute pixel coordinates are present, skip area-based logic.
    if max(abs(x1), abs(y1), abs(x2), abs(y2)) > 2.0:
        return None
    if x2 < x1 or y2 < y1:
        return None
    return (x1,y1,x2,y2)

def bbox_area(b):
    if not b:
        return None
    x1,y1,x2,y2 = b
    return max(0.0, x2-x1) * max(0.0, y2-y1)

def obvious_normal_video_ocr_score(labs):
    """Return (score, reasons). Only obvious large/readable text gets through."""
    best = 0.0
    reasons = []
    strong_labels = {'presentation slide','whiteboard','blackboard','screen','screen recording','phone screen','document','billboard','sign','text','subtitle'}
    for x in labs:
        lab = x.get('label','')
        cf = float(x.get('confidence',0) or 0)
        if lab not in TEXT_LABELS and lab not in strong_labels:
            continue
        area = bbox_area(x.get('bbox'))
        # large readable region: bbox area + confidence
        if area is not None:
            if area >= 0.035 and cf >= 0.45:
                sc = 2.2 + cf*3.0 + min(area, 0.35)*4.0
                best = max(best, sc)
                reasons.append(f'normal_video_large_text_label:{lab}:conf={cf:.2f}:area={area:.3f}')
            elif area >= 0.018 and cf >= 0.70 and lab in strong_labels:
                sc = 2.0 + cf*2.5 + area*3.0
                best = max(best, sc)
                reasons.append(f'normal_video_medium_strong_text_label:{lab}:conf={cf:.2f}:area={area:.3f}')
        else:
            # If bbox is unavailable, require high confidence and strong text-like label.
            if cf >= 0.82 and lab in {'presentation slide','whiteboard','blackboard','screen','document','billboard','sign','text'}:
                sc = 3.5 + cf
                best = max(best, sc)
                reasons.append(f'normal_video_high_conf_text_label:{lab}:conf={cf:.2f}:bbox_unavailable')
    return best, reasons

def final_video_dedup(q_rows, labels, min_gap_ms=18000, sim_threshold=0.72):
    """Dedup final video Qwen rows inside each source_content_id after V17+V14 merge."""
    video_cats = {'video_coverage_keyframe','video_coverage_high_signal_overlap','video_high_signal_supplement'}
    by_source = defaultdict(list)
    non_video = []
    for r in q_rows:
        if r.get('high_value_category') in video_cats:
            by_source[r.get('source_content_id')].append(r)
        else:
            non_video.append(r)

    dropped = []
    kept_all = []
    for sid, rows in by_source.items():
        rows = sorted(rows, key=lambda r: int(float(r.get('time_position_ms') or -1)))
        kept = []
        for r in rows:
            if not kept:
                kept.append(r); continue
            prev = kept[-1]
            gap = abs(int(float(r.get('time_position_ms') or -1)) - int(float(prev.get('time_position_ms') or -1)))
            a = labelset(labels.get(prev.get('visual_unit_id'), []))
            b = labelset(labels.get(r.get('visual_unit_id'), []))
            sim = jacc(a,b)
            same_visual_block = gap < int(min_gap_ms) and sim >= float(sim_threshold)
            very_close = gap < 9000 and sim >= 0.50
            if same_visual_block or very_close:
                # keep the better representative:
                # overlap > supplement > coverage, then score
                def priority(x):
                    cat = x.get('high_value_category')
                    pri = {'video_coverage_high_signal_overlap': 3, 'video_high_signal_supplement': 2, 'video_coverage_keyframe': 1}.get(cat, 0)
                    try: sc = float(x.get('candidate_score') or 0)
                    except Exception: sc = 0
                    return (pri, sc)
                if priority(r) > priority(prev):
                    dropped.append(prev)
                    kept[-1] = r
                else:
                    dropped.append(r)
            else:
                kept.append(r)
        kept_all.extend(kept)
    return non_video + kept_all, dropped


def read_csv_rows(path):
    p=Path(path)
    if not p.exists(): return []
    with p.open(newline='',encoding='utf-8') as f:
        return list(csv.DictReader(f))

def load_v14_video_high(v14_out):
    q=Path(v14_out)/'manifests'/'qwenvl_high_value_candidate_queue.csv'
    rows=read_csv_rows(q)
    out={}
    by_source=defaultdict(list)
    for r in rows:
        if r.get('high_value_category')!='video_high_value_segment_candidate':
            continue
        vu=r.get('visual_unit_id')
        sid=r.get('source_content_id')
        if not vu or not sid: continue
        try: r['_candidate_score_float']=float(r.get('candidate_score') or 0)
        except Exception: r['_candidate_score_float']=0.0
        try: r['_time_position_ms_int']=int(float(r.get('time_position_ms') or -1))
        except Exception: r['_time_position_ms_int']=-1
        out[vu]=r
        by_source[sid].append(r)
    for sid in list(by_source):
        by_source[sid].sort(key=lambda x:(-x.get('_candidate_score_float',0), x.get('_time_position_ms_int',-1)))
    return out,by_source

def min_gap_ms(frame, selected_frames):
    t=frame.get('time_position_ms',-1)
    if t is None or int(t)<0 or not selected_frames: return 10**12
    return min(abs(int(t)-int(x.get('time_position_ms',-1))) for x in selected_frames if x.get('time_position_ms',-1) is not None)

def supplement_cap_for_group(frame_count):
    if frame_count < 18: return 0
    if frame_count < 35: return 1
    if frame_count < 80: return 2
    return 3

def is_black(r):
    if r.get('near_black') in (1,'1',True,'true','True'): return True
    try:
        m=float(r.get('luma_mean')) if r.get('luma_mean') not in (None,'') else None
        s=float(r.get('luma_std')) if r.get('luma_std') not in (None,'') else None
        return m is not None and s is not None and m<=8 and s<=5
    except Exception: return False

def load_labels(c):
    out=defaultdict(list)
    if not table_cols(c,'visual_labels'): return out
    for r in c.execute('SELECT visual_unit_id,label,confidence,bbox FROM visual_labels'):
        lab=(r['label'] or '').strip().lower()
        if not lab: continue
        try: cf=float(r['confidence'] or 0)
        except Exception: cf=0
        out[r['visual_unit_id']].append({'label':lab,'confidence':cf,'bbox':parse_bbox(r['bbox'])})
    return out

def labelset(labs, minc=0.2): return {x['label'] for x in labs if x.get('confidence',0)>=minc}
def jacc(a,b): return 1.0 if not a and not b else (len(a&b)/max(1,len(a|b)) if a and b else 0.0)
def score_labels(labs):
    s=0.0; rs=[]
    for x in labs:
        lab=x['label']; cf=max(0.2,x.get('confidence',0)); w=WEIGHTS.get(lab,0)
        if w: s+=w*cf; rs.append(f'yoloe:{lab}:conf={x.get("confidence",0):.2f}')
        if lab in {'hand','finger','arm'}: s-=1.2*cf; rs.append(f'penalty:{lab}')
    if len(labelset(labs))>=3: s+=0.5; rs.append(f'visual_complexity:labels={len(labelset(labs))}')
    return s,rs

def visual_rows(c):
    sql='''SELECT vu.visual_unit_id,vu.source_content_id,vu.derived_id,vu.visual_file,vu.time_position_ms AS vu_time_position_ms,vu.near_black,vu.luma_mean,vu.luma_std,da.derived_path,da.time_position_ms AS da_time_position_ms,da.frame_index,sa.relative_path,sa.absolute_path,sa.media_type FROM visual_units vu LEFT JOIN derived_assets da ON da.derived_id=vu.derived_id LEFT JOIN source_assets sa ON sa.source_content_id=vu.source_content_id'''
    rows=[]
    for r in c.execute(sql):
        d=dict(r); t=d.get('da_time_position_ms')
        if t is None or str(t)=='' or int(t)<0: t=d.get('vu_time_position_ms')
        try: d['time_position_ms']=int(t) if t is not None and str(t)!='' else -1
        except Exception: d['time_position_ms']=-1
        d['source_relative_path']=d.get('relative_path') or d.get('absolute_path') or ''
        rows.append(d)
    return rows

def is_tail(f, frames):
    ts=[x['time_position_ms'] for x in frames if x.get('time_position_ms',-1)>=0]
    if not ts: return False
    dur=max(ts)-min(ts)
    if dur<=30000: return False
    win=int(max(6000,min(30000,dur*0.05)))
    return f.get('time_position_ms',-1)>=max(ts)-win

def center_indices(n,stride):
    if n<=0: return []
    c=(n-1)//2; out=[c]; k=1
    while True:
        ok=False
        for j in (c+k*stride,c-k*stride):
            if 0<=j<n: out.append(j); ok=True
        if not ok: break
        k+=1
    return sorted(set(out))

def replacement(idx,frames,blocked):
    for r in range(1,5):
        for j in (idx+r,idx-r):
            if 0<=j<len(frames) and j not in blocked and not is_black(frames[j]) and not is_tail(frames[j],frames): return j
    return None

def choose(frames, labels, stride):
    frames=sorted([f for f in frames if f.get('time_position_ms',-1)>=0], key=lambda x:(x['time_position_ms'],x['visual_unit_id']))
    stats=Counter(); chosen=[]; blocked=set()
    for idx in center_indices(len(frames),stride):
        f=frames[idx]
        if is_tail(f,frames):
            stats['tail_suppressed_signal_count']+=1; j=replacement(idx,frames,blocked)
            if j is None: continue
            idx=j; f=frames[idx]; stats['tail_replacement_count']+=1
        if is_black(f):
            stats['black_selected_initial_count']+=1; j=replacement(idx,frames,blocked)
            if j is None: stats['black_drop_no_replacement_count']+=1; continue
            idx=j; f=frames[idx]; stats['black_replacement_count']+=1
        if idx not in blocked: chosen.append(idx); blocked.add(idx)
    kept=[]
    for idx in sorted(set(chosen)):
        if not kept: kept.append(idx); continue
        prev=kept[-1]
        gap=abs(frames[idx]['time_position_ms']-frames[prev]['time_position_ms'])
        sim=jacc(labelset(labels.get(frames[idx]['visual_unit_id'],[])), labelset(labels.get(frames[prev]['visual_unit_id'],[])))
        if gap<12000 and sim>=0.85:
            stats['same_shot_dedup_drop_count']+=1
            if score_labels(labels.get(frames[idx]['visual_unit_id'],[]))[0] > score_labels(labels.get(frames[prev]['visual_unit_id'],[]))[0]: kept[-1]=idx
        else: kept.append(idx)
    return [frames[i] for i in kept], stats

def manifest(f,queue,cat,score,reasons,labels):
    labs=Counter(x['label'] for x in labels.get(f['visual_unit_id'],[]))
    return {'visual_unit_id':f['visual_unit_id'],'source_content_id':f['source_content_id'],'derived_id':f.get('derived_id'),'media_type':f.get('media_type'),'visual_unit_type':'video_frame' if f.get('media_type')=='video' else 'image','source_relative_path':f.get('source_relative_path',''),'visual_file':f.get('visual_file',''),'derived_path':f.get('derived_path',''),'time_position_ms':f.get('time_position_ms',-1),'source_group_id':f.get('source_content_id'),'queue_type':queue,'high_value_category':cat,'candidate_score':round(float(score),6),'reason_codes':'|'.join([str(x) for x in reasons if x]),'black_frame_status':'black' if is_black(f) else 'ok','luma_mean':f.get('luma_mean'),'luma_std':f.get('luma_std'),'label_count':sum(labs.values()),'labels':'|'.join(f'{k}:{v}' for k,v in labs.most_common()),'policy_version':POLICY_VERSION,'script_version':SCRIPT_VERSION}

def insert(c,queue,f,score,reasons,run_id):
    cid='cand_'+sha(f'{run_id}|{queue}|{f["visual_unit_id"]}|{score}|{"|".join(reasons)}')[:24]
    c.execute('''INSERT OR REPLACE INTO stop03_2_candidate_queue_items (candidate_id,queue_type,visual_unit_id,source_content_id,derived_id,candidate_score,reason_codes,black_frame_status,luma_mean,luma_std,run_id,script_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',(cid,queue,f['visual_unit_id'],f['source_content_id'],f.get('derived_id'),float(score),'|'.join(reasons),'black' if is_black(f) else 'ok',f.get('luma_mean'),f.get('luma_std'),run_id,SCRIPT_VERSION,now()))

def write_csv(p,rows,fields):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(rows)

def write_jsonl(p,rows):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open('w',encoding='utf-8') as f:
        for r in rows: f.write(json.dumps(r,ensure_ascii=False)+'\n')

def preflight(db,out):
    offline(); cs={}; blockers=[]
    if db.exists():
        c=conn(db)
        for t in ['visual_units','derived_assets','source_assets','visual_labels','model_runs','manual_high_value_visual_seeds','stop03_2_candidate_queue_items']:
            cs[t]=table_cols(c,t)
            if not cs[t]: blockers.append('missing_table:'+t)
    else: blockers.append('missing_db:'+str(db))
    return {'script_version':SCRIPT_VERSION,'validation_status':'PASS' if not blockers else 'FAIL','python_executable':sys.executable,'expected_python':str(EXPECTED_PY),'project_root':str(PROJECT_ROOT),'test_output_root':str(TEST_OUTPUT_ROOT),'db_path':str(db),'out_path':str(out),'tables':cs,'blockers':blockers,'offline_env':offline(),'safety':{'network':'blocked_by_offline_env_not_used','download':'not_used','model_loading':'not_used','source_media_write':'blocked'},'platform':platform.platform()}

def run(args):
    db=Path(args.db); out=Path(args.out); pre=preflight(db,out)
    if args.preflight_only: return {'validation_status':pre['validation_status'],'runtime_preflight':pre}
    if pre['validation_status']!='PASS': return {'validation_status':'FAIL','runtime_preflight':pre}
    c=conn(db); labels=load_labels(c); rows=visual_rows(c); by_vu={r['visual_unit_id']:r for r in rows}; by_video=defaultdict(list)
    v14_high_by_vu,v14_high_by_source=load_v14_video_high(args.v14_out)
    for r in rows:
        if r.get('media_type')=='video': by_video[r['source_content_id']].append(r)
    run_id=f'{SCRIPT_VERSION}_{datetime.now().strftime("%Y%m%d_%H%M%S")}_{os.getpid()}'
    if args.clear_existing_candidate_items: c.execute('DELETE FROM stop03_2_candidate_queue_items')
    q=[]; o=[]; stats=Counter(); video_budget=[]
    for sid,fs in by_video.items():
        path=fs[0].get('source_relative_path','') if fs else ''
        if is_screen(path):
            stats['screen_recording_qwenvl_disabled_group_count']+=1
            for f in sorted(fs,key=lambda x:(x.get('time_position_ms',-1),x.get('visual_unit_id',''))):
                if is_black(f): continue
                s,rr=score_labels(labels.get(f['visual_unit_id'],[])); rr=['strict_screen_capture_path','ocr_path_keyword:rpreplay' if 'rpreplay' in path.lower() else 'ocr_path_keyword:screen_capture']+rr
                row=manifest(f,'ocr_trigger','ocr_screen_capture_video_frame',max(3.25,s),rr,labels); row['ocr_trigger_source']='strict_screen_capture_path'; row['ocr_trigger_keywords']='rpreplay' if 'rpreplay' in path.lower() else 'screen_capture'; o.append(row); insert(c,'ocr_trigger',f,row['candidate_score'],rr,run_id)
        else:
            # V19: normal camera video OCR is allowed only for obvious large/readable text,
            # with a per-video cap and minimum time gap. Weak text-like YOLOE labels are excluded.
            normal_ocr_candidates = []
            for f in fs:
                labs_f = labels.get(f['visual_unit_id'], [])
                sc_ocr, rr_ocr = obvious_normal_video_ocr_score(labs_f)
                if sc_ocr >= float(args.normal_video_ocr_min_score) and not is_black(f):
                    normal_ocr_candidates.append((sc_ocr, f, rr_ocr))
                elif labelset(labs_f) & TEXT_LABELS:
                    stats['normal_video_ocr_weak_excluded_count'] += 1
            normal_ocr_candidates.sort(key=lambda x: (-x[0], int(x[1].get('time_position_ms', -1))))
            selected_normal_ocr = []
            for sc_ocr, f_ocr, rr_ocr in normal_ocr_candidates:
                if len(selected_normal_ocr) >= int(args.normal_video_ocr_cap):
                    stats['normal_video_ocr_cap_excluded_count'] += 1
                    continue
                if min_gap_ms(f_ocr, selected_normal_ocr) < int(args.normal_video_ocr_min_gap_ms):
                    stats['normal_video_ocr_near_duplicate_excluded_count'] += 1
                    continue
                rr_final = ['normal_video_obvious_large_text_ocr', f'normal_video_ocr_score:{sc_ocr:.3f}'] + rr_ocr
                row = manifest(f_ocr, 'ocr_trigger', 'ocr_normal_video_large_text_frame', max(3.25, sc_ocr), rr_final, labels)
                row['ocr_trigger_source'] = 'normal_video_obvious_large_text'
                row['ocr_trigger_keywords'] = 'large_text'
                o.append(row); insert(c, 'ocr_trigger', f_ocr, row['candidate_score'], rr_final, run_id)
                selected_normal_ocr.append(f_ocr)
                stats['normal_video_ocr_added_count'] += 1
            chosen,st=choose(fs,labels,args.video_stride); stats.update(st)
            selected_video_frames=[]
            selected_vus=set()
            for f in chosen:
                s,rr=score_labels(labels.get(f['visual_unit_id'],[]))
                overlap=f['visual_unit_id'] in v14_high_by_vu
                cat='video_coverage_high_signal_overlap' if overlap else 'video_coverage_keyframe'
                rr=['v17_time_coverage_keyframe',f'video_stride:{args.video_stride}']+(['v14_high_signal_overlap'] if overlap else [])+rr
                row=manifest(f,'qwenvl_high_value',cat,max(1.0,s),rr,labels); q.append(row); insert(c,'qwenvl_high_value',f,row['candidate_score'],rr,run_id)
                selected_video_frames.append(f); selected_vus.add(f['visual_unit_id'])
                if overlap: stats['v19_v17_v14_overlap_count']+=1
            # V14 supplement: add high-signal frames only when they fill a temporal gap from V17 coverage.
            cap=supplement_cap_for_group(len(fs)); added=0
            sorted_frames=sorted([x for x in fs if x.get('time_position_ms',-1)>=0], key=lambda x:x.get('time_position_ms',-1))
            for vr in v14_high_by_source.get(sid,[]):
                if added>=cap: break
                vu=vr.get('visual_unit_id')
                if not vu or vu in selected_vus: continue
                f=by_vu.get(vu)
                if not f or is_black(f) or is_tail(f, sorted_frames):
                    stats['v19_v14_supplement_reject_bad_or_tail_count']+=1
                    continue
                if min_gap_ms(f, selected_video_frames) < int(args.v14_supplement_min_gap_ms):
                    stats['v19_v14_supplement_reject_near_coverage_count']+=1
                    continue
                s,rr0=score_labels(labels.get(vu,[]))
                v14score=vr.get('_candidate_score_float',0.0)
                rr=['v14_high_signal_supplement',f'v14_score:{v14score:.3f}',f'min_gap_ms:{min_gap_ms(f,selected_video_frames)}']+rr0
                row=manifest(f,'qwenvl_high_value','video_high_signal_supplement',max(1.0,s,v14score),rr,labels); q.append(row); insert(c,'qwenvl_high_value',f,row['candidate_score'],rr,run_id)
                selected_video_frames.append(f); selected_vus.add(vu); added+=1; stats['v19_v14_supplement_added_count']+=1
        ts=[x.get('time_position_ms',-1) for x in fs if x.get('time_position_ms',-1)>=0]
        dur=(max(ts)-min(ts))/1000 if ts else 0
        video_budget.append({'source_content_id':sid,'source_relative_path':path,'step02_frame_count':len(fs),'selected_count':sum(1 for r in q if r.get('source_content_id')==sid),'duration_s':round(dur,3),'screen_capture_excluded_from_qwen':int(is_screen(path))})
    # manual image seeds
    if table_cols(c,'manual_high_value_visual_seeds'):
        for sr in c.execute('SELECT visual_unit_id,seed_label,reason FROM manual_high_value_visual_seeds'):
            f=by_vu.get(sr['visual_unit_id'])
            if f and not is_black(f):
                rr=['manual_finder_tag_image_seed',f'seed_label:{sr["seed_label"]}',str(sr['reason'] or '')]
                row=manifest(f,'qwenvl_high_value','manual_finder_tag_image_seed',100.0,rr,labels); q.append(row); insert(c,'qwenvl_high_value',f,100.0,rr,run_id)
    # timelapse DB
    if table_cols(c,'step02_image_timelapse_keyframes'):
        seq=defaultdict(list)
        for tr in c.execute('SELECT * FROM step02_image_timelapse_keyframes'):
            seq[str(tr['sequence_id'])].append(dict(tr))
        for sid,items in seq.items():
            pref=next((x for x in items if x.get('representative_position')=='middle'),None) or items[0]
            f=by_vu.get(pref.get('visual_unit_id'))
            if f and not is_black(f):
                s,rr0=score_labels(labels.get(f['visual_unit_id'],[])); rr=['step02_timelapse_keyframe_db_source',f'sequence_id:{sid}',f'representative_position:{pref.get("representative_position")}']+rr0
                row=manifest(f,'qwenvl_high_value','timelapse_candidate',max(1.0,s),rr,labels); row['source_group_id']='step02_timelapse_sequence:'+sid; q.append(row); insert(c,'qwenvl_high_value',f,row['candidate_score'],rr,run_id)
    # generic image, conservative
    qvu={r['visual_unit_id'] for r in q}
    for f in rows:
        if f.get('media_type')=='image' and f['visual_unit_id'] not in qvu and not is_black(f):
            s,rr0=score_labels(labels.get(f['visual_unit_id'],[]))
            if s>=args.image_yolo_threshold:
                rr=['image_generic_visual_signal_candidate']+rr0; row=manifest(f,'qwenvl_high_value','image_generic_visual_signal_candidate',s,rr,labels); q.append(row); insert(c,'qwenvl_high_value',f,row['candidate_score'],rr,run_id)
    # V19 final per-video dedup after V17 coverage + V14 supplement.
    q_before_final_dedup = len(q)
    q, final_dropped_rows = final_video_dedup(
        q,
        labels,
        min_gap_ms=int(args.final_video_dedup_min_gap_ms),
        sim_threshold=float(args.final_video_dedup_label_sim_threshold),
    )
    stats['v19_final_video_dedup_drop_count'] = len(final_dropped_rows)
    stats['v19_final_video_dedup_before_count'] = q_before_final_dedup
    stats['v19_final_video_dedup_after_count'] = len(q)

    fields=['visual_unit_id','source_content_id','derived_id','media_type','visual_unit_type','source_relative_path','visual_file','derived_path','time_position_ms','source_group_id','queue_type','high_value_category','candidate_score','reason_codes','black_frame_status','luma_mean','luma_std','label_count','labels','ocr_trigger_source','ocr_trigger_keywords','policy_version','script_version']
    outm=out/'manifests'; outr=out/'reports'; outm.mkdir(parents=True,exist_ok=True); outr.mkdir(parents=True,exist_ok=True)
    q=sorted(q,key=lambda r:(r.get('media_type',''),r.get('source_relative_path',''),int(r.get('time_position_ms') or -1)))
    o=sorted(o,key=lambda r:(r.get('source_relative_path',''),int(r.get('time_position_ms') or -1)))
    write_csv(outm/'qwenvl_high_value_candidate_queue.csv',q,fields); write_jsonl(outm/'qwenvl_high_value_candidate_queue.jsonl',q)
    write_csv(outm/'ocr_trigger_candidate_queue.csv',o,fields); write_jsonl(outm/'ocr_trigger_candidate_queue.jsonl',o)
    write_csv(outr/'video_budget_report.csv',video_budget,list(video_budget[0].keys()) if video_budget else [])
    blackq=sum(1 for r in q if r['black_frame_status']!='ok'); blacko=sum(1 for r in o if r['black_frame_status']!='ok'); screenq=sum(1 for r in q if r['high_value_category'] in ('video_coverage_keyframe','video_coverage_high_signal_overlap','video_high_signal_supplement') and is_screen(r.get('source_relative_path','')))
    summary={'validation_status':'PASS' if blackq==0 and blacko==0 and screenq==0 else 'FAIL','policy_version':POLICY_VERSION,'script_version':SCRIPT_VERSION,'run_id':run_id,'input_visual_units':len(rows),'input_video_visual_units':sum(1 for r in rows if r.get('media_type')=='video'),'input_image_visual_units':sum(1 for r in rows if r.get('media_type')=='image'),'qwenvl_total_count':len(q),'qwen_video_frame_count':sum(1 for r in q if r['high_value_category'] in ('video_coverage_keyframe','video_coverage_high_signal_overlap','video_high_signal_supplement')),'qwen_manual_seed_count':sum(1 for r in q if r['high_value_category']=='manual_finder_tag_image_seed'),'qwen_timelapse_count':sum(1 for r in q if r['high_value_category']=='timelapse_candidate'),'qwen_image_yoloe_count':sum(1 for r in q if r['high_value_category']=='image_generic_visual_signal_candidate'),'ocr_total_count':len(o),'qwen_category_counts':dict(Counter(r['high_value_category'] for r in q)),'ocr_media_type_counts':dict(Counter(r.get('media_type') for r in o)),'black_leak_into_qwenvl_count':blackq,'black_leak_into_ocr_count':blacko,'screen_recording_qwenvl_leak_count':screenq,'screen_recording_qwenvl_disabled_group_count':stats['screen_recording_qwenvl_disabled_group_count'],'screen_capture_video_frame_input_count':len(o),'normal_video_ocr_default_excluded_count':stats['normal_video_ocr_weak_excluded_count'],'normal_video_ocr_added_count':stats['normal_video_ocr_added_count'],'normal_video_ocr_cap_excluded_count':stats['normal_video_ocr_cap_excluded_count'],'normal_video_ocr_near_duplicate_excluded_count':stats['normal_video_ocr_near_duplicate_excluded_count'],'tail_suppressed_signal_count':stats['tail_suppressed_signal_count'],'tail_replacement_count':stats['tail_replacement_count'],'black_replacement_count':stats['black_replacement_count'],'same_shot_dedup_drop_count':stats['same_shot_dedup_drop_count'],'v19_v17_v14_overlap_count':stats['v19_v17_v14_overlap_count'],'v19_v14_supplement_added_count':stats['v19_v14_supplement_added_count'],'v19_v14_supplement_reject_near_coverage_count':stats['v19_v14_supplement_reject_near_coverage_count'],'v19_v14_supplement_reject_bad_or_tail_count':stats['v19_v14_supplement_reject_bad_or_tail_count'],'v19_final_video_dedup_drop_count':stats['v19_final_video_dedup_drop_count'],'v19_final_video_dedup_before_count':stats['v19_final_video_dedup_before_count'],'v19_final_video_dedup_after_count':stats['v19_final_video_dedup_after_count'],'video_source_group_count':len(by_video),'video_selected_group_count':sum(1 for r in video_budget if int(r['selected_count'])>0),'video_stride':args.video_stride,'settings':{'selection_mode':'v19_v17_base_plus_v14_supplement_final_video_dedup_selective_normal_video_ocr_db_only','video_stride':args.video_stride,'v14_out':str(args.v14_out),'v14_supplement_min_gap_ms':args.v14_supplement_min_gap_ms,'final_video_dedup_min_gap_ms':args.final_video_dedup_min_gap_ms,'final_video_dedup_label_sim_threshold':args.final_video_dedup_label_sim_threshold,'normal_video_ocr_cap':args.normal_video_ocr_cap,'normal_video_ocr_min_score':args.normal_video_ocr_min_score,'normal_video_ocr_min_gap_ms':args.normal_video_ocr_min_gap_ms,'image_yolo_threshold':args.image_yolo_threshold},'safety':pre['safety'],'runtime_preflight':pre,'outputs':{'qwenvl_csv':str(outm/'qwenvl_high_value_candidate_queue.csv'),'ocr_csv':str(outm/'ocr_trigger_candidate_queue.csv'),'summary_json':str(outr/'stop03_2_candidate_summary.json'),'video_budget_report_csv':str(outr/'video_budget_report.csv')},'model_rerun':{'yoloe':False,'openclip':False,'qwen_vl':False,'ocr':False}}
    (outr/'stop03_2_candidate_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    (outr/'stop03_2_candidate_summary.md').write_text('\n'.join([f'- **{k}**: `{summary.get(k)}`' for k in ['validation_status','qwenvl_total_count','qwen_video_frame_count','ocr_total_count','screen_recording_qwenvl_leak_count','black_leak_into_qwenvl_count','tail_suppressed_signal_count','same_shot_dedup_drop_count','v19_v17_v14_overlap_count','v19_v14_supplement_added_count']]),encoding='utf-8')
    c.execute('''INSERT OR REPLACE INTO model_runs (run_id,stage,model_name,model_path,script_version,script_path,input_count,output_count,status,started_at,finished_at,error_message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(run_id,'stop03_2_candidate_queues','rule_based_db_v19_v17_base_v14_supplement_final_dedup_selective_ocr_no_model','',SCRIPT_VERSION,__file__,len(rows),len(q)+len(o),'done',now(),now(),''))
    c.commit(); return summary

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--db',default=str(DEFAULT_DB)); ap.add_argument('--out',default=str(DEFAULT_OUT)); ap.add_argument('--v14-out',default=str(DEFAULT_V14_OUT)); ap.add_argument('--preflight-only',action='store_true'); ap.add_argument('--clear-existing-candidate-items',action='store_true'); ap.add_argument('--image-yolo-threshold',type=float,default=4.2); ap.add_argument('--video-stride',type=int,default=6); ap.add_argument('--v14-supplement-min-gap-ms',type=int,default=15000); ap.add_argument('--final-video-dedup-min-gap-ms',type=int,default=18000); ap.add_argument('--final-video-dedup-label-sim-threshold',type=float,default=0.72); ap.add_argument('--normal-video-ocr-cap',type=int,default=1); ap.add_argument('--normal-video-ocr-min-score',type=float,default=4.25); ap.add_argument('--normal-video-ocr-min-gap-ms',type=int,default=20000)
    res=run(ap.parse_args()); print(json.dumps(res,ensure_ascii=False,indent=2)); return 0 if res.get('validation_status')=='PASS' else 2
if __name__=='__main__': raise SystemExit(main())
