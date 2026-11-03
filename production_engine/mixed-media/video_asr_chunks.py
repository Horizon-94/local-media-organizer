"""Pure continuous-window planner. Original VAD evidence is never changed."""
import copy,hashlib,json,math
CONFIG={'revision':'video-continuous-asr-chunks-v1','merge_gap_ms':700,'short_extension_gap_ms':2000,
        'padding_ms':700,'minimum_window_ms':3000,'soft_window_ms':15000,'hard_max_ms':20000,'vad_rounding_tolerance_ms':1}

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def build_chunks(intervals,duration_ms,*,source_revision='',config=None):
    cfg={**CONFIG,**(config or {})};duration=float(duration_ms)
    if not math.isfinite(duration) or duration<=0:raise ValueError('ASR_SOURCE_DURATION_INVALID')
    maximum=cfg['hard_max_ms'];padding=cfg['padding_ms'];minimum=cfg['minimum_window_ms']
    if not 0<minimum<=maximum or maximum>20000 or padding<0:raise ValueError('ASR_CHUNK_CONFIG_INVALID')
    groups=[];last=-1
    for index,raw in enumerate(intervals):
        a,b=raw['start_ms'],raw['end_ms']
        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in (a,b)) or not 0<=a<b<=duration+cfg['vad_rounding_tolerance_ms'] or a<last:raise ValueError('ASR_VAD_MASK_INVALID')
        b=min(b,duration)  # planning intersection only; original_interval stays exact
        last=a;start=a
        while start<b:
            end=min(b,start+maximum)
            member={'vad_ordinal':index,'original_interval':copy.deepcopy(raw),'included_start_ms':start,'included_end_ms':end}
            if groups:
                g=groups[-1];gap=start-g['end'];span=end-g['start']
                short=(g['end']-g['start']<minimum or end-start<minimum)
                merge=(gap<=cfg['merge_gap_ms'] and g['end']-g['start']<cfg['soft_window_ms']) or (short and gap<=cfg['short_extension_gap_ms'])
                if merge and span<=maximum:
                    g['end']=max(g['end'],end);g['members'].append(member)
                    g['reasons'].append('SHORT_CONTEXT_EXTENSION' if gap>cfg['merge_gap_ms'] else 'ADJACENT_SPEECH_MASK')
                    start=end;continue
            reason='HARD_MAX_SPLIT' if end<b or start>a else 'INITIAL_SPEECH_MASK'
            groups.append({'start':start,'end':end,'members':[member],'reasons':[reason]+(['VAD_SAMPLE_ROUNDING_BOUNDARY'] if raw['end_ms']>duration else [])})
            start=end
    chunks=[]
    for n,g in enumerate(groups):
        left=max(0,g['start']-padding);right=min(duration,g['end']+padding)
        if right-left<minimum:
            missing=minimum-(right-left);left=max(0,left-missing/2);right=min(duration,left+minimum);left=max(0,right-minimum)
            g['reasons'].append('SHORT_CONTEXT_EXTENSION')
        if right-left>maximum:
            spare=maximum-(g['end']-g['start']);left=max(0,g['start']-spare/2);right=min(duration,left+maximum)
        # A midpoint inside the actual silence gap gives disjoint ownership.
        # Context is never concatenated, reordered, or silently double-owned.
        if n:left=max(left,(groups[n-1]['end']+g['start'])/2)
        if n+1<len(groups):right=min(right,(g['end']+groups[n+1]['start'])/2)
        if not left<=g['start']<g['end']<=right or right-left>maximum:raise ValueError('ASR_CHUNK_BOUNDARY_INVALID')
        row={'chunk_start_ms':left,'chunk_end_ms':right,'start_ms':left,'end_ms':right,
             'included_vad_intervals':g['members'],'speech_duration_ms':sum(m['included_end_ms']-m['included_start_ms'] for m in g['members']),
             'continuous_window_duration_ms':right-left,'padding_before_ms':g['start']-left,'padding_after_ms':right-g['end'],
             'merge_reason':sorted(set(g['reasons'])),'chunking_config':cfg}
        row['chunk_id']=fingerprint((source_revision,cfg,n,row));chunks.append(row)
    return chunks

def speech_mask_review(timeline,intervals,*,tolerance_ms=350,minimum_non_speech_ms=1200):
    flags=[]
    for s in timeline:
        a,b=s['source_start_ms'],s['source_end_ms']
        if b-a<minimum_non_speech_ms:continue
        if not any(min(b,m['end_ms']+tolerance_ms)>max(a,m['start_ms']-tolerance_ms) for m in intervals):flags.append('VAD_SPEECH_MISMATCH')
    return sorted(set(flags))

def deduplicate_overlapping(rows):
    kept=[];duplicates=[]
    for row in rows:
        text=''.join(row['text'].split()).casefold();a,b=row['source_start_ms'],row['source_end_ms']
        prior=next((p for p in kept if ''.join(p['text'].split()).casefold()==text and text and
                    max(0,min(b,p['source_end_ms'])-max(a,p['source_start_ms']))>=.6*min(b-a,p['source_end_ms']-p['source_start_ms'])),None)
        if prior is None:kept.append(row)
        else:duplicates.append({'row':row,'duplicate_of':prior})
    return kept,duplicates
