"""Opt-in native workers for isolated VIDEO streaming acceptance, not App install.

Configs are supplied explicitly. No inherited production output/cache path is
allowed. Lazy load is a separate lifecycle cost, outside service-time timing.
"""
import copy
from pathlib import Path
import threading
import time
import subprocess
import hashlib
import json,sqlite3,zlib
from .model_resource_monitor import ModelResourceMonitor
from .v2_real_handlers import SubprocessModelHandler
from .v2_model_workers import create_handler
from .video_ocr_compact import CONTRACT


class Resident:
    def __init__(self,stage,config,monitor):
        self.stage=stage;self.config=config;self.monitor=monitor
        self.handler=None;self.lock=threading.Lock();self.cold_start_ms=None
        self.last_used=0;self.closed=False
        self.first_call=True;self.service_time_unknown=False
        self.yolo_terms=None
    def prepare(self):
        with self.lock:
            if self.closed:raise RuntimeError('native_handler_already_closed')
            if self.handler is None:
                start=time.monotonic()
                self.handler=SubprocessModelHandler(self.stage,self.config,resource_monitor=self.monitor)
                self.cold_start_ms=(time.monotonic()-start)*1000
    @staticmethod
    def normalized(stage,result):
        # EXACT existing production wording normalization, not schema fallback.
        if stage=='qwen' and result.get('raw',{}).get('truncation_status')=='not_truncated':
            return {**result,'raw':{**result['raw'],'truncation_status':'complete'}}
        return result
    def run_job(self,job):
        context={k:job.get(k) for k in ("source_id","revision","job_id")}
        context["attempt"]=job.get("attempts")
        return self(job["payload"],_audit_context=context)
    def __call__(self,payload,_audit_context=None):
        self.prepare()
        # mlx_whisper loads lazily inside its first inference. That combined
        # cost is explicitly excluded from steady ETA, not mislabeled service.
        self.service_time_unknown=self.stage=='whisper' and self.first_call
        if self.stage in ('vad','whisper') and payload.get('representation')=='source-native-transient-v1':
            from .audio_pcm_input import convert
            target=convert(payload,Path(self.config['_event_log']).parent/'audio-model-inputs',self.stage,_audit_context)
            payload={**payload,'path':str(target)}
        try:
            result=self.handler(payload)
            if self.stage=='yoloe' and 'label_terms' in result and 'registry' in result:
                from .video_result_storage import registry_semantic_sha
                semantic=registry_semantic_sha(result)
                if self.yolo_terms is None:
                    database=Path(self.config['_event_log']).parent/'video.sqlite'
                    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as c:
                        for (body,) in c.execute('SELECT body FROM yolo_registry ORDER BY rowid'):
                            static=json.loads(zlib.decompress(body))
                            if registry_semantic_sha(static)==semantic:
                                self.yolo_terms=static['label_terms'];break
                    if self.yolo_terms is None:self.yolo_terms=result['label_terms']
                if registry_semantic_sha({'registry':result['registry'],'label_terms':self.yolo_terms})!=semantic:raise ValueError('frozen_yolo_registry_semantics_changed')
                result={**result,'label_terms':self.yolo_terms}
            return self.normalized(self.stage,result)
        finally:self.last_used=time.monotonic();self.first_call=False
    def batch(self,payloads):
        self.prepare()
        try:return self.handler.batch(payloads)
        finally:self.last_used=time.monotonic()
    def idle(self):
        # Called only by THIS consumer between jobs. Never retires inference.
        if self.handler is not None and self.last_used and time.monotonic()-self.last_used>60:
            with self.lock:
                self.handler.close();self.handler=None
                self.first_call=True
    def close(self):
        with self.lock:
            self.closed=True
            if self.handler is not None:self.handler.close();self.handler=None


def isolated_configs(configs,workspace):
    specs=copy.deepcopy(configs);root=Path(workspace).resolve()
    q=specs['qwen']
    expected={'max_tokens':384,'temperature':0.,'top_p':1.,'repetition_penalty':1.1,'repetition_context_size':64}
    if any(q.get(k)!=v for k,v in expected.items()):raise ValueError('native_qwen_frozen_parameters_mismatch')
    if Path(q['model_path']).name!='Qwen3.5-9B-4bit':raise ValueError('native_qwen_model_mismatch')
    q.pop('output_truncation_retry_policy',None)
    q['source_wave_retry']=True
    specs['openclip']['device']='cpu'
    specs['ocr']['durable_contract']=CONTRACT
    specs['ocr']['paddlex_cache_root']=str(root/'paddlex_cache')
    specs['audio_extract']['artifact_root']=str(root/'audio')
    for stage,spec in specs.items():
        # Existing debug/log locations never escape the experiment workspace.
        for key in list(spec):
            if key.startswith('_') and ('log' in key or 'cache' in key):spec.pop(key)
        spec['_event_log']=str(root/'native-worker-events.jsonl')
        if stage=='qwen':spec['_inference_event_log']=str(root/'native-qwen-events.jsonl')
    return specs


def handlers(specs,*,handler_type=Resident,monitor=None):
    monitor=monitor or ModelResourceMonitor()
    values={s:handler_type(s,specs[s],monitor) for s in ('yoloe','openclip','insightface','qwen','ocr','vad','whisper','embedding')}
    values['frame_basics']=[create_handler('frame_basics',{}) for _ in range(2)]
    # Source was already demuxed ONCE; this is a durable artifact publication,
    # never the legacy original-video audio extractor.
    values['audio_extract']=lambda p:{k:v for k,v in p.items() if not k.startswith('_')}
    return values,monitor
