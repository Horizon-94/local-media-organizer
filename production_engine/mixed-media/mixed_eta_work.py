"""Count only facts for the same media kind; adaptive future work is a range."""
from collections import defaultdict
def qwen_work(v,plan):
    typed=v.get('stages_by_kind',{});counts=v.get('qwen_candidate_counts_by_kind',{})
    selected=set(v.get('selected_source_ids_by_kind',{}).get('image',[]))
    published=set(v.get('qwen_candidate_sources_by_kind',{}).get('image',[]))
    forced={r['source_content_id'] for r in plan.get('finder_tags',[])};future_forced=forced-selected
    groups=defaultdict(set)
    for row in plan.get('decisions',[]):
        if row.get('sequence_id') and row.get('should_generate_preview') and row.get('preview_role')=='timelapse_keyframe':groups[str(row['sequence_id'])].add(row['parent_source_content_id'])
    low=high=0;ranges={}
    for gid,members in groups.items():
        unknown=members-selected;unforced=unknown-future_forced
        minimum=int(bool(unknown) and not members&published and not unknown&future_forced)
        maximum=len(unforced);ranges[gid]=[minimum,maximum];low+=minimum;high+=maximum
    result={}
    for kind in ('image','video'):
        statuses=typed.get(kind,{}).get('qwen',{})
        n=lambda x:x.get('count',0) if isinstance(x,dict) else x
        total=sum(n(x) for x in statuses.values());done=n(statuses.get('DONE',0));failed=n(statuses.get('TERMINAL_FAILED',0))
        materialized=max(total,counts.get(kind,0));remaining=max(0,materialized-done-failed)
        future=[len(future_forced)+low,len(future_forced)+high] if kind=='image' else [0,0]
        result[kind]={'materialized':materialized,'done':done,'terminal_failed':failed,'current_remaining':remaining,
                      'known_future_range':future,'known_remaining_range':[remaining+future[0],remaining+future[1]]}
    return result,ranges

def closure_work(pending,observed):
    by_gate=defaultdict(int);reasons=[];cost=0
    for source in pending.values():by_gate[source.get('media_kind','unknown')+':'+source.get('next_gate','index_projection_closure')]+=1
    ops={'image':('final_projection','visual_closure'),'video':('final_projection','visual_closure','source_closure'),'audio':('audio_source_closure',)}
    for key,count in by_gate.items():
        kind,gate=key.split(':',1)
        if gate!='index_projection_closure':
            if gate=='representative_coverage':reasons.append('COVERAGE_MEMBER_COST_NOT_OBSERVED')
            continue
        if kind=='unknown' and 'closure' in observed:
            s=observed['closure'];cost+=count*s['P95_ms_per_item']/1000/max(.1,1-s['stale_rate']);continue
        for operation in ops.get(kind,()):
            s=observed.get('closure:'+kind+':'+operation)
            if not s:reasons.append(kind.upper()+'_'+operation.upper()+'_SERVICE_NOT_OBSERVED');continue
            cost+=count*s['P95_ms_per_item']/1000/max(.1,1-s['stale_rate'])
    return cost,dict(by_gate),reasons
