"""Versioned usable ASR facts. Raw evidence is never edited or silently clamped."""
import copy,hashlib,json,math,re,threading,time
from collections import Counter,defaultdict

CONTRACT='source-global-usable-asr-v1'
POLICY={'contract':CONTRACT,'boundary_tolerance_ms':40,'same_character_limit':20,
        'pattern_min_characters':40,'pattern_repeat_limit':8,'max_provider_calls':2,
        'context_padding_ms':350,'guard_revision':'strict-model-metrics-and-corruption-v2'}
HARD={'PATHOLOGICAL_REPETITION','INVALID_ALIGNMENT','CORRUPTED_TEXT','INVALID_MODEL_METRICS','REPAIR_OWNERSHIP_UNRESOLVED'}

def corrupted(text):
    # Punctuation cannot hide a replacement-only transcript; retain U+FFFD
    # explicitly before the general non-word/repetition cleaning step.
    chars=[c for c in (text or '') if c.isalnum() or c=='\ufffd']
    return bool(chars) and chars.count('\ufffd')/len(chars)>=.5

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def repetition(text):
    compact=re.sub(r'[\W_]+','',text or '')
    if re.search(r'(.)\1{19,}',compact):return 'PATHOLOGICAL_REPETITION'
    if re.search(r'(.{2,12})\1{7,}',compact) and len(compact)>=40:return 'PATHOLOGICAL_REPETITION'
    if len(compact)>=24 and re.search(r'(.{3,12})\1{3,}',compact):return 'REVIEW'
    return None

def evaluate(result,clip_start_ms,clip_end_ms,*,offset_ms=0,speaker_id=None):
    """Raw subsegment seconds -> a separate validated source-global ms timeline."""
    if not 0<=clip_start_ms<clip_end_ms:raise ValueError('ASR_OWNING_CLIP_INVALID')
    raw=copy.deepcopy(result);flags=[];alignment=[];usable=[]
    segments=result.get('raw',{}).get('segments',[])
    if not isinstance(segments,list):segments=[]
    if not segments and result.get('text','').strip():flags.append('INVALID_ALIGNMENT')
    last_end=clip_start_ms
    for n,segment in enumerate(segments):
        text=segment.get('text','');a=segment.get('start');b=segment.get('end')
        status='VALID';start=end=None
        if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (a,b)):
            status='TIMESTAMP_INVALID'
        else:
            start=round(a*1000)+offset_ms;end=round(b*1000)+offset_ms
            if end<start:status='NEGATIVE_DURATION'
            elif end==start:
                status='EMPTY_ZERO_DURATION' if not text.strip() else 'TIMESTAMP_INVALID'
            elif start<clip_start_ms-40 or end>clip_end_ms+40 or start<last_end-40:
                status='TIMESTAMP_INVALID'
            elif start<clip_start_ms or end>clip_end_ms or start<last_end:
                status='BOUNDARY_NORMALIZED';start=max(start,clip_start_ms,last_end);end=min(end,clip_end_ms)
                if end<=start:status='TIMESTAMP_INVALID'
        alignment.append({'raw_ordinal':n,'classification':status,'source_start_ms':start,'source_end_ms':end})
        if status in ('NEGATIVE_DURATION','TIMESTAMP_INVALID'):
            if status=='NEGATIVE_DURATION':flags.append('NEGATIVE_DURATION')
            flags.append('INVALID_ALIGNMENT');continue
        if status=='EMPTY_ZERO_DURATION':continue
        last_end=end
        if text.strip():
            if result.get('text_normalization'):
                from apps.media_archive_image_video_ui.chinese_text import POLICY as TEXT_POLICY, simplified_chinese
                if result['text_normalization'] != TEXT_POLICY:raise ValueError('ASR_TEXT_NORMALIZATION_UNKNOWN')
                text = simplified_chinese(text)
            row={'source_start_ms':start,'source_end_ms':end,'text':text.strip(),
                 'language':result.get('language'),'quality_status':status,'raw_ordinal':n}
            if speaker_id is not None:row['speaker_id']=speaker_id
            usable.append(row)
    rep=repetition(result.get('text',''))
    if any(repetition(s.get('text',''))=='PATHOLOGICAL_REPETITION' for s in segments):rep='PATHOLOGICAL_REPETITION'
    if rep:flags.append(rep)
    if corrupted(result.get('text','')) or any(corrupted(s.get('text','')) for s in segments):flags.append('CORRUPTED_TEXT')
    for s in segments:
        for key in ('avg_logprob','compression_ratio','no_speech_prob'):
            if key in s and (isinstance(s[key],bool) or not isinstance(s[key],(int,float)) or not math.isfinite(s[key])):
                flags.append('INVALID_MODEL_METRICS')
        lp=s.get('avg_logprob')
        if isinstance(lp,(int,float)) and math.isfinite(lp) and lp < -1:flags.append('LOW_CONFIDENCE')
    quality=next((q for q in ('CORRUPTED_TEXT','INVALID_MODEL_METRICS','PATHOLOGICAL_REPETITION','INVALID_ALIGNMENT','REVIEW','LOW_CONFIDENCE') if q in flags),'PASS')
    # Even a valid-looking subset cannot make an invalid clip appear accepted.
    if quality in HARD:usable=[]
    return {'quality':quality,'flags':sorted(set(flags)),'alignment':alignment,
            'usable_timeline':usable,'usable_text':' '.join(s['text'] for s in usable),
            'raw_sha256':digest(raw)}

def assess_attempt(raw,left,right,*,offset=0,input_payload=None):
    repair=(input_payload or {}).get('_video_repair')
    if not repair:return evaluate(raw,left,right,offset_ms=offset)
    from video_asr_repair import owned_assessment
    return owned_assessment(raw,left,right,offset,repair)

class RepeatHistory:
    """Source-local diagnostic repetition; short legitimate replies never hard-fail."""
    def __init__(self):self.lock=threading.Lock();self.values=defaultdict(Counter);self.seen=set()
    def observe(self,source,segment,text):
        key=re.sub(r'[\W_]+','',text.casefold())
        with self.lock:
            if (source,segment) not in self.seen:
                self.seen.add((source,segment));self.values[source][key]+=1
            return len(key)>=8 and self.values[source][key]>=3

def guarded_inference(provider,payload,local_input,*,source_id=None,history=None):
    """At most A + one B call. Both outputs and input provenance remain durable."""
    left,right=payload['start_ms'],payload['end_ms'];attempts=[]
    started=time.time();first=provider(payload);ended=time.time();assessment=evaluate(first,left,right)
    attempts.append({'provider':'whisper-large-v3-turbo-mlx','path':'A_FULL_PCM_CLIP','input':
                     {k:payload.get(k) for k in ('path','sha','start_ms','end_ms')},'raw_result':copy.deepcopy(first),'assessment':assessment,
                     'started_at':started,'finished_at':ended,'service_ms':(ended-started)*1000})
    chosen=0;derived=[]
    fallback_error=None
    if assessment['quality'] in HARD:
        second_payload=None;fallback_started=time.time()
        try:
            second_payload,offset,owned=local_input(payload);derived=owned
            started=time.time();second=provider(second_payload);ended=time.time();next_assessment=assess_attempt(second,left,right,offset=offset,input_payload=second_payload)
            attempts.append({'provider':'whisper-large-v3-turbo-mlx','path':'B_LOCAL_PCM_CONTEXT','why_retried':assessment['quality'],
                             'input':copy.deepcopy(second_payload),'source_offset_ms':offset,'raw_result':copy.deepcopy(second),'assessment':next_assessment,
                             'started_at':started,'finished_at':ended,'service_ms':(ended-started)*1000})
            if next_assessment['quality'] not in HARD:chosen=1;assessment=next_assessment
        except Exception as error:
            # Stop is cancellation, not a completed rejected transcript. Let the
            # existing native lease/retry path retain this job for Resume.
            if str(error)=='STOP_BEFORE_CHILD_SPAWN':raise
            # Keep A durable even if constructing/inferring B fails. It remains
            # rejected, never a successful transcript or an unbounded retry.
            fallback_error={'type':type(error).__name__,'reason':str(error),'at':time.time(),'path':'B_LOCAL_PCM_CONTEXT',
                'provider':'whisper-large-v3-turbo-mlx','input':copy.deepcopy(second_payload),
                'phase':'PROVIDER_OR_VALIDATION' if second_payload is not None else 'LOCAL_INPUT_SETUP',
                'why_retried':assessment['quality'],'service_ms':(time.time()-fallback_started)*1000}
    if history and assessment['quality'] not in HARD and history.observe(source_id,payload.get('segment_id',digest(payload)),assessment['usable_text']):
        assessment=copy.deepcopy(assessment);assessment['flags'].append('CROSS_SEGMENT_SUSPICIOUS_REPEAT')
        if assessment['quality']=='PASS':assessment['quality']='REVIEW'
    return {**copy.deepcopy(first),'asr_contract':CONTRACT,'asr_policy':copy.deepcopy(POLICY),
            'source_start_ms':left,'source_end_ms':right,'quality_status':assessment['quality'],
            'quality_flags':assessment['flags'],'usable_text':assessment['usable_text'],
            'usable_timeline':assessment['usable_timeline'],'selected_asr_attempt':chosen+1,
            'asr_attempts':attempts,'fallback_error':fallback_error,'derived_inputs':derived,'raw_evidence_sha256':digest(first)}

def validate_packet(result):
    if result.get('asr_contract')!=CONTRACT:raise ValueError('ASR_USABLE_CONTRACT_REQUIRED')
    if not 1<=len(result.get('asr_attempts',[]))<=2:raise ValueError('ASR_BOUNDED_CALLS_INVALID')
    if result['raw_evidence_sha256']!=digest(result['asr_attempts'][0]['raw_result']):raise ValueError('ASR_RAW_EVIDENCE_CHANGED')
    original=result['asr_attempts'][0]['raw_result']
    if digest(result.get('raw'))!=digest(original.get('raw')) or result.get('text')!=original.get('text'):raise ValueError('ASR_RAW_WRAPPER_CHANGED')
    if result['quality_status'] in HARD and (result['usable_timeline'] or result['usable_text']):raise ValueError('INVALID_ASR_MUST_NOT_BE_USABLE')
    left,right=result['source_start_ms'],result['source_end_ms'];previous=left
    choice=result['selected_asr_attempt']-1
    if not 0<=choice<len(result['asr_attempts']):raise ValueError('ASR_SELECTED_ATTEMPT_INVALID')
    selected=result['asr_attempts'][choice]
    expected=assess_attempt(selected['raw_result'],left,right,offset=selected.get('source_offset_ms',0),input_payload=selected.get('input'))
    if result['quality_status']!=expected['quality'] and not (expected['quality']=='PASS' and result['quality_status']=='REVIEW' and 'CROSS_SEGMENT_SUSPICIOUS_REPEAT' in result['quality_flags']):
        raise ValueError('ASR_QUALITY_RAW_LINEAGE_MISMATCH')
    if result['usable_timeline']!=expected['usable_timeline'] or result['usable_text']!=expected['usable_text']:
        raise ValueError('ASR_USABLE_RAW_LINEAGE_MISMATCH')
    repair=selected.get('input',{}).get('_video_repair')
    if repair:left,right=repair['window_ms'];previous=left
    for s in result['usable_timeline']:
        if not previous<=s['source_start_ms']<s['source_end_ms']<=right:raise ValueError('ASR_USABLE_BOUNDS_INVALID')
        previous=s['source_end_ms']
    if result['usable_text']!=' '.join(s['text'] for s in result['usable_timeline']):raise ValueError('ASR_USABLE_TEXT_MISMATCH')
