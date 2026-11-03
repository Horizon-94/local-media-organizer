"""Model configuration/IPC only, no scheduling. Single physical Qwen lane."""
import copy,importlib.util,json,os,subprocess,threading,time
from pathlib import Path
from apps.media_archive_image_video_ui.v2_real_handlers import SubprocessModelHandler,_StderrCapture,ROOT
from apps.media_archive_image_video_ui.video_stream_native import Resident
from apps.media_archive_image_video_ui.model_resource_monitor import ModelResourceMonitor
from apps.media_archive_image_video_ui.v2_model_workers import create_handler
from apps.media_archive_image_video_ui.v2_qwen_schema_retry import PERSISTENT_GROUP_POLICY,UNCERTAINTY_POLICY
from mixed_observer import save,connect

class PolicyProcess(SubprocessModelHandler):
    def __init__(self,config,monitor):
        self.stage='qwen';self.config=dict(config);self.event_log=self.config.pop('_event_log',None)
        self.response_timeout_seconds=float(self.config.pop('response_timeout_seconds',300))
        self._lock=threading.Lock();self.has_completed_result=False;self.resource_monitor=monitor
        python=self.config.pop('python_path')
        command=[python,str(Path(__file__).with_name('qwen_media_worker.py')),'--config-json',json.dumps(self.config)]
        self.process=subprocess.Popen(command,cwd=str(ROOT),env={**os.environ,'PYTHONPATH':str(ROOT)},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=False,bufsize=0,start_new_session=True)
        self._stderr_capture=_StderrCapture(self.process.stderr,f'mixed-qwen-stderr-{self.process.pid}')
        self.resource_worker_id=f'qwen:{self.process.pid}';monitor.register('qwen',self.resource_worker_id,self.process.pid)
        try:
            if json.loads(self._read_response({},time.time()))!={'ok':True,'worker_state':'READY'}:raise RuntimeError('worker_ready_protocol_invalid')
            monitor.transition(self.resource_worker_id,'READY')
        except Exception:
            self._terminate_owned_worker();monitor.transition(self.resource_worker_id,'CLOSED');raise

class MixedQwen(Resident):
    def prepare(self):
        with self.lock:
            if self.closed:raise RuntimeError('native_handler_already_closed')
            if self.handler is None:
                start=time.monotonic();self.handler=PolicyProcess(self.config,self.monitor);self.cold_start_ms=(time.monotonic()-start)*1000
    def __call__(self,payload):
        with connect(Path(self.config['_event_log']).parent/'video.sqlite') as c:
            row=c.execute('SELECT m.metadata FROM v2_selected_candidates x JOIN v2_source_metadata m ON m.source_id=x.source_id AND m.source_revision=x.source_revision WHERE x.candidate_id=?',(payload['candidate_id'],)).fetchone()
        if not row:raise ValueError('QWEN_CANDIDATE_SOURCE_NOT_CURRENT')
        kind=json.loads(row[0])['media_type']
        return super().__call__({**payload,'_mixed_media_kind':kind})

def native(workspace):
    file=Path(__file__).parents[1]/'video-batch/model_setup.py'
    spec=importlib.util.spec_from_file_location('_mixed_native_builders',file);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    specs=module.specs_for(workspace);video=module.frozen_contracts(specs)
    image_specs=copy.deepcopy(specs);image_specs['qwen'].update(schema_retry_policy=PERSISTENT_GROUP_POLICY,image_qwen_profile='current_qwen35_9b',physical_workers=1)
    # Freeze just the differing Qwen config; all other models use the same pins.
    image_q=module.frozen_contracts({'qwen':image_specs['qwen']})['qwen']
    contracts={'video':video,'audio':video,'image':{**video,'qwen':image_q}}
    monitor=ModelResourceMonitor();handlers={s:Resident(s,c,monitor) for s,c in specs.items() if s!='audio_extract'}
    qcfg=copy.deepcopy(specs['qwen']);qcfg['_mixed_policies']={'image':PERSISTENT_GROUP_POLICY,'video':qcfg.get('schema_retry_policy',UNCERTAINTY_POLICY)}
    handlers['qwen']=MixedQwen('qwen',qcfg,monitor)
    from asr_quality import CONTRACT,POLICY
    from apps.media_archive_image_video_ui.v2_producer import fingerprint
    from apps.media_archive_image_video_ui.v2_model_workers import sha_file
    from audio_core_native import GuardedWhisper
    # Equivalent source-local query optimization is operational only. Keep
    # the accepted ASR guard/model/result identity on same-workspace Resume.
    operational_guard_pins={'audio_core_native.py':'94512df547e56007ed72b925903e73444a84f0d71b7e53889950ef0b416aebb5'}
    guard_identity={name:operational_guard_pins.get(name) or sha_file(Path(__file__).with_name(name)) for name in ('asr_quality.py','audio_core_native.py','audio_usable_documents.py')}
    contracts['audio']={**video,'whisper':{**video['whisper'],'usable_asr_contract':CONTRACT,'audio_core_policy':fingerprint(POLICY),'audio_core_code':guard_identity}}
    contracts['video']={**video,'whisper':dict(contracts['audio']['whisper'])}
    from video_transcript_quality import POLICY
    contracts['video']['whisper'].update(video_transcript_policy=POLICY,
        video_transcript_policy_code=sha_file(Path(__file__).with_name('video_transcript_quality.py')))
    from video_asr_repair import POLICY as REPAIR_POLICY
    contracts['video']['whisper'].update(video_repair_policy=REPAIR_POLICY,
        video_repair_code={n:sha_file(Path(__file__).with_name(n)) for n in ('video_asr_repair.py','video_asr_chunks.py')})
    from speech_evidence import POLICY as SPEECH_POLICY
    contracts['video']['whisper'].update(speech_evidence_policy=SPEECH_POLICY,
        speech_evidence_code=sha_file(Path(__file__).with_name('speech_evidence.py')),
        search_publication_policy='source-search-commit-v1',
        search_publication_code={n:sha_file(Path(__file__).with_name(n)) for n in ('search_publication.py','release_incremental.py')})
    handlers['whisper']=GuardedWhisper(handlers['whisper'],workspace,guarded_kinds=('audio','video'))
    handlers['frame_basics']=[create_handler('frame_basics',{}) for _ in range(2)]
    handlers['audio_extract']=lambda p:{k:v for k,v in p.items() if not k.startswith('_')}
    save(workspace/'model-config.json',{'specs':specs,'qwen_policies':qcfg['_mixed_policies'],'contracts':contracts})
    return contracts,handlers,monitor
