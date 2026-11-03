"""Build150 admission/report helpers around the formal shared Runtime."""
from collections import Counter
import json,math,shutil,sqlite3
from pathlib import Path

CALIBRATION={'encoded_max_bytes':2097152,'sqlite_bytes':800000000,'policy':'frozen-build150-conservative-deployment-bound'}

def release_rows(inv):
    rows=[r for r in inv['files'] if r['media_kind'] in ('image','video')]
    counts=Counter(r['media_kind'] for r in inv['files'])
    return rows,{'standalone_audio':counts['audio']+counts['unsupported_audio'],
                 'unsupported_sources':counts['unsupported_image']+counts['unsupported_video'],
                 'other_files':sum(v for k,v in counts.items() if k not in ('image','video','audio','unsupported_audio','unsupported_image','unsupported_video'))}

def print_discovery(rows,ignored):
    n=Counter(r['media_kind'] for r in rows)
    for k,v in {'IMAGE_DISCOVERED':n['image'],'VIDEO_DISCOVERED':n['video'],
                'STANDALONE_AUDIO_IGNORED':ignored.get('standalone_audio',0),
                'IGNORED_UNSUPPORTED_SOURCE_COUNT':ignored.get('unsupported_sources',0),
                'EXPECTED_SOURCE_TOTAL':len(rows)}.items():print(k,'=',v,flush=True)

def release_storage_budget(rows,plan,run):
    from apps.media_archive_image_video_ui.v2_producer import SamplingPolicy
    from mixed_observer import read
    images=sum(d['should_generate_preview'] for d in plan['decisions'])
    frames=0;max_audio=0;open_audio_bound=0;max_frames=0;pending_metadata=0
    for r in rows:
        if r['media_kind']!='video':continue
        if r.get('probe_pending'):
            pending_metadata+=1;continue
        meta=r['probe']['metadata'];seconds=float(meta['format']['duration'])
        source_frames=(1 if seconds<2 else SamplingPolicy().count(round(seconds*1000)))+1
        frames+=source_frames;max_frames=max(max_frames,source_frames)
        audio=next((s for s in meta['streams'] if s.get('codec_type')=='audio'),None)
        if audio:
            size=math.ceil(seconds*(int(audio['sample_rate'])*int(audio['channels'])*4+16000*4))
            max_audio=max(max_audio,size);open_audio_bound+=size
    sizes=[CALIBRATION['encoded_max_bytes']]
    ref={'status':'FROZEN_CALIBRATION'}
    # Empirical recommendation, not a claim about simultaneous model peaks.
    # Keep the existing hard 8GiB reader spool allowance and largest source
    # audio cache separately. The Runtime may hold many extracted but not-yet-
    # closed sources; it does not enforce a single open audio source. Therefore
    # the safe transient bound includes all still-open source caches, while
    # none of these bytes are charged as permanent retained audio.
    jpeg_per_item=max(sizes)*2
    db_budget=max(2*1024**3,CALIBRATION['sqlite_bytes']*4)
    persistent=(images+frames)*jpeg_per_item+db_budget
    transient=8*1024**3+open_audio_bound
    logs=1024**3;safety=4*1024**3;required=persistent+transient+logs+safety
    anchor=Path(run)
    while not anchor.exists():anchor=anchor.parent
    free=shutil.disk_usage(anchor).free
    forecast=required
    required=working_set_required(max_frames*jpeg_per_item,max_audio)
    return {'pending_media_metadata':pending_metadata,'forecast_complete':pending_metadata==0,'per_source_space_check_required':True,'available':free,'required':required,'forecast_total_bytes':forecast,'forecast_is_hard_requirement':False,'enough_space':free>=required,
            'shortfall':max(0,required-free),'reason_code':'OK' if free>=required else 'STORAGE_SHORTFALL',
            'estimate_policy':'per-source-working-set-v1; conservative total retained as advisory',
            'estimated_persistent_bytes':persistent,'estimated_max_transient_bytes':transient,
            'monitor_log_allowance_bytes':logs,'safety_margin_bytes':safety,
            'components':{'previews_and_frames':(images+frames)*jpeg_per_item,'db_wal_index':db_budget,'spool_hard_limit':8*1024**3,'outstanding_source_audio_cache_bound':open_audio_bound,'monitor_logs':logs,'safety':safety},
            'evidence':{'workspace':'BUNDLED_CALIBRATION','observed_encoded_files':len(sizes),'observed_max_encoded_bytes':max(sizes),'observed_encoded_total_bytes':sum(sizes),'historical_sqlite_bytes':CALIBRATION['sqlite_bytes'],'historical_report_status':ref.get('status')},
            'preview_required':images,'video_frames_with_guard':frames,
            'largest_single_source_audio_bytes':max_audio,
            'limitations':'Startup and per-source admission use bounded working set. Total forecast is advisory, not measured allocation. Empirical conservative recommendation; not proof of historical peak or maximum future WAL. Audio cache is transient but multiple unclosed sources may coexist. Runtime spool hard limit remains unchanged.'}

def acceptance(run):
    from mixed_observer import read,save
    root=Path(run);report=read(root/'reports/FINAL_BATCH_REPORT.json',{})
    expected=report.get('expected_sources',{})
    valid=report.get('status')=='PASS_BATCH' and not expected.get('audio',0)
    db=root/'runtime/video.sqlite'
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as c:
        foreign=c.execute("SELECT count(*) FROM v2_source_metadata WHERE json_extract(metadata,'$.media_type') NOT IN ('image','video')").fetchone()[0]
        orphans=c.execute('SELECT count(*) FROM video_stream_jobs_v1 j LEFT JOIN v2_source_metadata s ON s.source_id=j.source_id AND s.source_revision=j.revision WHERE s.source_id IS NULL').fetchone()[0]
        from search_publication import status as publication_status
        publication=publication_status(c)
        publication_pending=c.execute('SELECT count(*) FROM release_registry WHERE present=1 AND committed=0').fetchone()[0]
        leak=c.execute("SELECT count(*) FROM v2_documents d JOIN release_transcript_evidence e ON e.execution_key=d.raw_result_ref WHERE e.tier<>'SPEECH_CONFIDENT'").fetchone()[0]
        from apps.media_archive_image_video_ui.v2_producer import fingerprint
        embedding_leak=0
        for key,sha in c.execute("SELECT execution_key,result_sha FROM release_transcript_evidence WHERE tier<>'SPEECH_CONFIDENT'"):
            embedding_leak+=c.execute("SELECT count(*) FROM v2_current_jobs WHERE stage='embedding' AND item_id=?",(fingerprint(('whisper',key,sha)),)).fetchone()[0]
        speech=dict(c.execute('SELECT tier,count(*) FROM release_transcript_evidence GROUP BY tier'))
    valid=valid and foreign==0 and orphans==0 and publication_pending==0 and leak==embedding_leak==0
    result={**report,'release_scope':'IMAGE_VIDEO_ONLY','standalone_registered':foreign,'orphan_jobs':orphans,
            'search_publication':publication,'publication_pending':publication_pending,'speech_evidence':speech,
            'primary_document_leak':leak,'primary_embedding_leak':embedding_leak,
            'status':'FULL_IMAGE_VIDEO_PASS' if valid else 'FULL_IMAGE_VIDEO_FAIL'}
    save(root/'reports/FULL_IMAGE_VIDEO_ACCEPTANCE.json',result)
    (root/'reports/FULL_IMAGE_VIDEO_ACCEPTANCE.md').write_text(result['status']+'\n\n```json\n'+json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n')
    print(result['status'],flush=True)
    return valid


def working_set_required(source_frames_bytes,source_audio_bytes):
    # Retain existing 8GiB spool ceiling + 4GiB safety + DB/log headroom.
    return 8*1024**3+4*1024**3+2*1024**3+1024**3+source_frames_bytes+source_audio_bytes

def require_source_space(row,run):
    from apps.media_archive_image_video_ui.v2_producer import SamplingPolicy
    frames=1;audio_bytes=0
    if row['media_kind']=='video':
        metadata=row['probe']['metadata'];seconds=float(metadata['format']['duration'])
        frames=(1 if seconds<2 else SamplingPolicy().count(round(seconds*1000)))+1
        audio=next((s for s in metadata['streams'] if s.get('codec_type')=='audio'),None)
        if audio:audio_bytes=math.ceil(seconds*(int(audio['sample_rate'])*int(audio['channels'])*4+16000*4))
    required=working_set_required(frames*CALIBRATION['encoded_max_bytes']*2,audio_bytes)
    free=shutil.disk_usage(run).free
    if free<required:raise OSError(28,'SOURCE_WORKING_SET_SHORTFALL: '+json.dumps({'available':free,'required':required,'path':row['path'],'policy':'per-source working set; source unchanged'}),str(run))
    return {'available':free,'required':required}
