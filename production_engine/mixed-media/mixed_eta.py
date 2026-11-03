"""Known-work critical-path estimate. No source-completion-rate extrapolation."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
def estimate(v,plan,manifest):
    if v.get('status')=='PASS_BATCH':return {'status':'COMPLETE','remaining':0,'range_seconds':[0,0],'confidence':'HIGH'}
    costs=v.get('runtime',{}).get('service_stats',{});reasons=[];branches={}
    def pending(stage):return sum(s.get('count',0) for k,s in v.get('stages',{}).get(stage,{}).items() if k in ('PENDING','RUNNING','RETRY_PENDING'))
    def done(stage):return v.get('stages',{}).get(stage,{}).get('DONE',{}).get('count',0)
    def service(stage):
        n=costs.get(stage,{}).get('EWMA_ms_per_item')
        if n is None:reasons.append(stage.upper()+'_SERVICE_NOT_OBSERVED')
        return n/1000 if n is not None else None
    images=sum(bool(d['should_generate_preview']) for d in plan.get('decisions',[]))
    video_frames=0
    for row in manifest.get('files',[]):
        if row['media_kind']=='video':
            from apps.media_archive_image_video_ui.v2_producer import SamplingPolicy
            if row.get('probe_pending') or not row.get('probe',{}).get('ok'):
                reasons.append('VIDEO_MEDIA_METADATA_PENDING');continue
            duration=float(row['probe']['metadata']['format']['duration'])
            video_frames+=1 if duration<2 else SamplingPolicy().count(round(duration*1000))
    visual=images+video_frames
    observed=v.get('preparation_costs',{}).get('stages',{})
    states=v.get('states',{})
    preview_remaining=max(0,images-states.get('IMAGE_PREVIEW_READY',0))
    if preview_remaining:
        if 'preview' in observed:branches['image_preview']=preview_remaining*observed['preview']['mean_ms_per_item']/1000
        else:reasons.append('PREVIEW_SERVICE_NOT_OBSERVED')
    if states.get('EXTRACTION_DONE',0)<sum(r['media_kind']=='video' for r in manifest.get('files',[])):reasons.append('VIDEO_EXTRACTION_SERVICE_NOT_YET_BOUNDED')
    visual_known=[]
    for stage in ('frame_basics','yoloe','openclip','insightface'):
        remaining=max(0,visual-done(stage));cost=service(stage) if remaining else 0
        if cost is not None:visual_known.append(remaining*cost)
    if visual_known:branches['shared_visual']=max(visual_known)
    from mixed_eta_work import qwen_work,closure_work
    work,group_ranges=qwen_work(v,plan)
    qremaining=sum(r['known_remaining_range'][0] for r in work.values())
    qmaximum=sum(r['known_remaining_range'][1] for r in work.values())
    if not v.get('stages_by_kind'):qremaining=max(qremaining,pending('qwen'));qmaximum=max(qmaximum,pending('qwen'))
    qcost=service('qwen') if qmaximum else 0
    if qcost is not None:branches['shared_qwen']=qremaining*qcost # frozen concurrency=1
    for stage in ('whisper','embedding'):
        remaining=pending(stage);cost=service(stage) if remaining else 0
        workers=v.get('lanes',{}).get('worker_count',{}).get(stage,1)
        if cost is not None:branches['shared_'+stage]=remaining*cost/max(1,workers)
    # Whisper/VAD work not yet published must never disappear from confidence.
    audio_sources=sum(r['media_kind'] in ('video','audio') and any(s.get('codec_type')=='audio' for s in r.get('probe',{}).get('metadata',{}).get('streams',[])) for r in manifest.get('files',[]))
    if done('vad')<audio_sources:reasons.append('FUTURE_VAD_SEGMENT_COUNT_UNKNOWN')
    selection_done=v.get('selection_done',0)
    selected_sources=images+sum(r['media_kind']=='video' for r in manifest.get('files',[]))
    if selection_done<selected_sources:reasons.append('FUTURE_VISUAL_SELECTION_NOT_SEALED')
    closure_gates={}
    if v.get('closure_pending'):
        docs=sum(n for status,n in v.get('documents',{}).items() if status!='SKIP_NO_TEXT')
        coverage=v.get('coverage',{})
        missing=sum(max(0,docs-coverage.get(k,0)) for k in ('fts','vector'))+max(0,visual-coverage.get('visual-vector',0))
        if 'index' in observed:
            index=observed['index']
            branches['index']=missing*index['P95_ms_per_item']/1000/max(.1,1-index['stale_rate'])
        elif missing:reasons.append('INDEX_SERVICE_NOT_OBSERVED')
        closure,closure_gates,unknown=closure_work(v['closure_pending'],observed)
        branches['projection_closure']=closure;reasons.extend(unknown)
        if v.get('documents',{}).get('READY',0) or pending('qwen') or pending('whisper') or pending('ocr'):
            reasons.append('FUTURE_DOCUMENT_INDEX_WORK_NOT_FULLY_MATERIALIZED')
    lower=max(branches.values(),default=0);upper=max(lower,qmaximum*(qcost or 0));reasons=list(dict.fromkeys(reasons))
    # Only a range when all material producers are sealed and branch service
    # samples exist. Otherwise expose the measurable lower bound, not fake ETA.
    return {'status':'ESTIMATE' if not reasons else 'INSUFFICIENT_DATA','range_seconds':[lower*.9,upper*1.5] if not reasons else None,
            'known_work_estimate_seconds':lower,'estimate_scope':'OBSERVED_SERVICE_SCENARIO_NOT_MATHEMATICAL_LOWER_BOUND','confidence':'MEDIUM' if not reasons else 'LOW',
            'reasons':reasons,'branches_seconds':branches,'qwen_work_by_kind':work,'timelapse_future_candidate_ranges':group_ranges,'closure_pending_by_gate':closure_gates,
            'qwen_remaining_including_known_future':qremaining,'effective_qwen_workers':1,
            'critical_bottleneck':max(branches,key=branches.get) if branches else 'insufficient_data'}
