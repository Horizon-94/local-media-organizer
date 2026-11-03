"""Opt-in resident workers wrapping existing production libraries.

Importing this module never imports inference libraries or loads models. Each
process owns ONE handler; deployment supplies pinned local paths and contracts.
No resource scheduler, network download, or production DB writes live here.
"""
from __future__ import annotations
import hashlib
import importlib.util
import json
import os
import time
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from .v2_producer import fingerprint
from .v2_runtime import ContractError
from .v2_selection import original_v25,CONFIG_PATH,ROOT

SCRIPTS=ROOT/'scripts/03_stop03_visual_analysis'
ENTRY={
 'yoloe':'stop03_yoloe_full_from_db_safe_v6_20260709_170200.py',
 'openclip':'stop03_1b_openclip_visual_embedding_db_safe_v4_20260709_161500.py',
 'ocr':'stop03_4_ocr_db_orchestrator_v1.py',
 'qwen':'stop03_3e_qwenvl_persistent_runner_v1.py',
 'embedding':'stop03_5d_text_embedding_db_orchestrator_v1.py',
 'audio_extract':'../04_media_archive_app/run_audio_search_pilot_v1.py',
 'vad':'../04_media_archive_app/run_audio_search_pilot_v1.py'}
ENTRY['insightface']='../04_media_archive_app/stop03_1c_person_reid_db_orchestrator_v1.py'

def sha_file(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def local_dependencies(paths):
    """Fingerprint transitive repository imports without importing model code."""
    import ast
    pending=[Path(p).resolve() for p in paths];seen={}
    while pending:
        path=pending.pop()
        if path in seen:continue
        seen[path]=sha_file(path)
        if path.suffix!='.py':continue
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            names=[];base=path.parent
            if isinstance(node,ast.Import):names=[n.name for n in node.names]
            elif isinstance(node,ast.ImportFrom):
                if node.level:
                    for _ in range(node.level-1):base=base.parent
                names=[node.module or '']
                names += ['.'.join(filter(None,(node.module,n.name))) for n in node.names]
            for name in names:
                for anchor in (base,ROOT):
                    target=anchor.joinpath(*name.split('.'))
                    for candidate in (target.with_suffix('.py'),target/'__init__.py'):
                        if candidate.is_file() and ROOT in candidate.resolve().parents:pending.append(candidate.resolve())
    return {str(p.relative_to(ROOT)) if ROOT in p.parents else str(p):sha for p,sha in sorted(seen.items())}


def contract(stage,config,revision):
    """Hash actual code, config, prompt, and local weights; no fake model hash."""
    paths=list(config.get('fingerprint_paths',[]))
    keys={'yoloe':['model_path','mobileclip_path'],'openclip':['model_path'],'ocr':['text_detection_model_dir','text_recognition_model_dir'],
          'qwen':['model_path'],'embedding':['model_path'],'vad':['package_root'],'whisper':['model_path'],'insightface':['model_dir']}
    if stage in keys:
        allowed=Path(config['allowed_model_root']).resolve(strict=True) if config.get('allowed_model_root') else None
        if not allowed:raise ContractError('allowed_model_root_required')
        for key in keys[stage]:
            p=Path(config[key])
            if not p.is_absolute():raise ContractError('absolute_model_path_required')
            p=p.resolve(strict=True)
            if p!=allowed and allowed not in p.parents:raise ContractError('model_outside_allowed_root')
            paths.append(str(p))
    paths+= [config[key] for key in ('prompt_path','registry') if key in config]
    digests={}
    for name in paths:
        p=Path(name).resolve(strict=True)
        if not p.is_absolute():raise ContractError('absolute_path_required')
        files=sorted(x for x in p.rglob('*') if x.is_file()) if p.is_dir() else [p]
        digests[str(p)]={str(f.relative_to(p)) if p.is_dir() else f.name:sha_file(f) for f in files}
    if stage in ('yoloe','openclip','ocr','qwen','embedding','vad','whisper','insightface') and not digests:
        raise ContractError('model_fingerprints_required')
    entrypoints=[Path(__file__)]
    if stage in ENTRY:entrypoints.append(SCRIPTS/ENTRY[stage])
    if stage in ('quality','dhash'):
        entrypoints += [ROOT/'scripts/source_frame_dedup_central_db.py',
                       ROOT/'scripts/03_stop03_visual_analysis/stop03_2_candidate_queues_from_db_safe_v25_0_20260711.py',
                       CONFIG_PATH,ROOT/'configs/static_shot_representative_policy_v1.json']
    dependencies=local_dependencies(entrypoints)
    for key in ('ffmpeg','ffprobe'):
        if key in config:
            executable=Path(config[key])
            if not executable.is_file():raise ContractError('absolute_existing_tool_required:'+key)
            dependencies[str(executable.resolve())]=sha_file(executable)
    code=fingerprint(dependencies)
    from importlib.metadata import version,PackageNotFoundError
    packages={}
    for name in ('torch','ultralytics','open_clip_torch','paddleocr','paddlepaddle','mlx','mlx-vlm','mlx-whisper','opencv-python','sentence-transformers','Pillow','numpy','silero-vad','soundfile','librosa'):
        try:packages[name]=version(name)
        except PackageNotFoundError:pass
    identity={}
    if stage=='qwen':
        from .video_qwen_contract import contract_revision
        identity={'qwen_schema_version':'raw_media_v3','qwen_contract_revision':contract_revision()}
    return {**identity,'revision':revision,'code':code,'config':fingerprint(config),'model_files':digests,'packages':packages,'python':sys.version,
            'execution_contract':'pipeline-v2-local-worker-v2','stage':stage,'dependencies':dependencies,
            'preprocess':fingerprint(('existing-production-preprocess',dependencies,packages)),
            'prompt':sha_file(config['prompt_path']) if config.get('prompt_path') else None}

def module(stage):
    path=(SCRIPTS/ENTRY[stage]).resolve()
    # Legacy modules import siblings by name. No model initialization on import.
    if str(path.parent) not in sys.path:sys.path.insert(0,str(path.parent))
    spec=importlib.util.spec_from_file_location('v2_local_'+stage,path)
    m=importlib.util.module_from_spec(spec);sys.modules[spec.name]=m;spec.loader.exec_module(m)
    return m

def image_path(p):return str(p.get('image_path') or p['derived_path'])

def create_handler(stage,cfg):
    if stage=='frame_basics':
        # One queue item / one durable commit; preserve each frozen algorithm.
        handlers={name:create_handler(name,cfg) for name in ('dhash','geometry','quality')}
        return lambda p:{name:handler(p) for name,handler in handlers.items()}
    if stage=='insightface':
        m=module(stage);m.block_network();backend=m.OpenCVDnnBackend(cfg)
        return lambda p:{'faces':backend.infer(p['frame_id'],Path(image_path(p)))}
    if stage in ('quality','dhash','geometry'):
        def cheap(p):
            from PIL import Image,ImageFilter,ImageStat
            with Image.open(image_path(p)) as im:
                im.load();w,h=im.size;gray=im.convert('L')
                if stage=='geometry':return {'width':w,'height':h,'orientation':'portrait' if h>w else 'landscape','rotation':'UNKNOWN'}
                if stage=='dhash':
                    path=ROOT/'scripts/source_frame_dedup_central_db.py'
                    spec=importlib.util.spec_from_file_location('v2_dhash_contract',path)
                    dedup=importlib.util.module_from_spec(spec);spec.loader.exec_module(dedup)
                    result=dedup.dhash_and_sharpness({'visual_file':image_path(p)},'pillow')
                    if result['decode_error']:raise ContractError(result['decode_error'])
                    return {'dhash':result['dhash'],'sharpness_score':result['sharpness_score'],'contract':dedup.SCRIPT_VERSION}
                v25=original_v25();policy=json.loads(CONFIG_PATH.read_text())
                result=v25.fingerprint_one({'visual_unit_id':p['frame_id'],'visual_file':image_path(p)},policy)
                if result['signature_status']!='PASS':raise ContractError('quality_signature_failed')
                return {**result,'brightness':result['luma_mean_actual'],'black_ratio':result['black_pixel_ratio'],
                        'sharpness':ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0],
                        'sharpness_contract':'PIL_FIND_EDGES_variance_v1'}
        return cheap
    if stage=='yoloe':
        m=module(stage);classes,registry=m.load_classes(Path(cfg['registry']),cfg['include_b'],cfg['include_ocr_trigger'])
        terms,_=m.build_label_terms_from_registry(Path(cfg['registry']),cfg['include_b'],cfg['include_ocr_trigger'])
        m.worker_init(cfg['model_path'],cfg['mobileclip_path'],classes,cfg['device'])
        def yolo(p):
            raw=m.worker_predict_one((1,1,{**p,'visual_file':image_path(p),'visual_unit_id':p['frame_id']},cfg['imgsz'],cfg['confidence'],cfg['device']))
            if raw['status']!='success':raise ContractError(raw['error_message'])
            return {'detections':[{**d,'bbox':d['bbox_xyxy']} for d in raw['detections']],'raw':raw,'registry':registry,'label_terms':terms}
        return yolo
    if stage=='openclip':
        if cfg.get('query_text_only') and cfg['model_name']=='ViT-B-32' and cfg['device']=='cpu':
            from .query_clip_text import create
            return create(cfg['model_path'],cfg['model_name'],cfg['device'])
        m=module(stage);m.init_worker(cfg['model_path'],cfg['model_name'],cfg['device'])
        if cfg.get('query_text_only'):
            import torch,open_clip
            tokenizer=open_clip.get_tokenizer(cfg['model_name'])
            def query_clip(p):
                with torch.inference_mode():
                    vector=m.MODEL.encode_text(tokenizer([p['text']]).to(m.DEVICE)).float()
                    vector=vector/vector.norm(dim=-1,keepdim=True)
                return {'vector':vector[0].cpu().tolist()}
            return query_clip
        def clip(p):
            raw=m.infer_one({**p,'visual_file':image_path(p),'visual_unit_id':p['frame_id']})
            if raw['status']!='success':raise ContractError(raw.get('error_message','openclip_failed'))
            return {'vector':raw['embedding_vector'],'raw':raw}
        def clip_batch(payloads):
            # One encode_image call, not a loop over single-image inference.
            import torch,time,math
            from PIL import Image
            if not payloads or len(payloads)>8:raise ContractError('invalid_openclip_batch_size')
            started=time.perf_counter();started_at=m.now_iso();tensors=[];paths=[];digests=[]
            for p in payloads:
                path=Path(image_path(p));paths.append(path);digests.append(m.sha256_file(path))
                if p.get('derived_sha') and p['derived_sha']!=digests[-1]:raise ContractError('batch_artifact_digest_mismatch')
                with Image.open(path) as image:tensors.append(m.PREPROCESS(image.convert('RGB')))
            with torch.no_grad():
                vectors=m.MODEL.encode_image(torch.stack(tensors).to(m.DEVICE))
                vectors=vectors/vectors.norm(dim=-1,keepdim=True)
            vectors=vectors.detach().cpu().float().numpy().tolist()
            if len(vectors)!=len(payloads):raise ContractError('batch_result_cardinality_mismatch')
            outputs=[]
            for p,path,digest,vector in zip(payloads,paths,digests,vectors):
                packed=json.dumps(vector,ensure_ascii=False,separators=(',',':'))
                raw={**p,'visual_file':str(path),'visual_unit_id':p['frame_id'],
                    'script_version':m.SCRIPT_VERSION,'worker_pid':m.os.getpid(),'device':m.DEVICE,
                    'model_name':m.MODEL_NAME,'model_path':m.MODEL_PATH,'status':'success',
                    'start_time':started_at,'end_time':m.now_iso(),'elapsed_ms':round((time.perf_counter()-started)*1000,3),
                    'visual_file_sha256':digest,'embedding_dim':len(vector),
                    'embedding_norm':round(math.sqrt(sum(x*x for x in vector)),8),
                    'embedding_vector_sha256':hashlib.sha256(packed.encode()).hexdigest(),
                    'embedding_vector':vector,'error_message':'','batch_contract':'openclip_true_batch_v1'}
                outputs.append({'vector':vector,'raw':raw})
            return outputs
        clip.batch=clip_batch
        return clip
    if stage=='ocr':
        m=module(stage);m.init_ocr_worker(cfg)
        def ocr(p):
            raw=m.to_jsonable(list(m._OCR_ENGINE.predict(input=image_path(p))))
            lines=m.extract_lines(raw)
            if cfg.get('durable_contract') == 'video-ocr-text-lines-v1':
                from .video_ocr_compact import compact_result
                return compact_result({'text':'\n'.join(line['text'] for line in lines),'lines':lines})
            return {'text':'\n'.join(line['text'] for line in lines),'lines':lines,'raw':raw}
        return ocr
    if stage=='qwen':
        m=module(stage)
        # New-model inference has exactly one output schema. Legacy parsers
        # remain for reading historical evidence, never as a 3.5 fallback.
        model_config_path=Path(cfg['model_path'])/'config.json'
        model_config=json.loads(model_config_path.read_text()) if model_config_path.is_file() else {}
        prompt=Path(cfg['prompt_path']).read_text()
        from qwenvl_raw_media_v3 import is_raw_media_prompt
        if not is_raw_media_prompt(prompt):
            raise ContractError('qwen_requires_raw_media_v3_13_fields')
        runner=m.PersistentQwenGenerationAdapter(model_path=Path(cfg['model_path']),
            max_tokens=cfg['max_tokens'],temperature=cfg['temperature'],top_p=cfg['top_p'],
            repetition_penalty=cfg.get('repetition_penalty'),repetition_context_size=cfg.get('repetition_context_size',20));runner.load_once()
        def qwen(p):
            timing_start=time.perf_counter();generation_start=timing_start
            def generation_event(kind):
                if not cfg.get('_inference_event_log'):return
                event={'kind':kind,'pid':os.getpid(),'candidate_id':p['candidate_id'],'time':time.time()}
                fd=os.open(cfg['_inference_event_log'],os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
                try:os.write(fd,(json.dumps(event)+'\n').encode())
                finally:os.close(fd)
            generation_event('inference_start')
            retry_audit = None
            schema_audit = None
            try:
                if cfg.get('source_wave_retry'):
                    if cfg.get('output_truncation_retry_policy'):
                        raise ContractError('source_wave_must_not_inflate_tokens')
                    from .v2_qwen_schema_retry import UNCERTAINTY_POLICY,generate_wave_attempt
                    outcome,schema_audit=generate_wave_attempt(runner,
                        candidate_id=p['candidate_id'],image_path=image_path(p),prompt=prompt,
                        attempt_number=p.get('_wave_attempt',1),previous_outcome=p.get('_previous_outcome'),
                        operator_retry=p.get('_operator_retry'),
                        policy=cfg.get('schema_retry_policy',UNCERTAINTY_POLICY))
                elif cfg.get('schema_retry_policy'):
                    from .v2_qwen_schema_retry import POLICY,UNCERTAINTY_POLICY,ORDERED_UNCERTAINTY_POLICY,generate
                    if cfg['schema_retry_policy'] not in (POLICY,UNCERTAINTY_POLICY,ORDERED_UNCERTAINTY_POLICY):raise ContractError('unknown_qwen_schema_retry_policy')
                    outcome,schema_audit=generate(runner,candidate_id=p['candidate_id'],image_path=image_path(p),prompt=prompt,policy=cfg['schema_retry_policy'])
                elif cfg.get('output_truncation_retry_policy'):
                    from .v2_qwen_truncation_retry import POLICY, generate
                    if cfg['output_truncation_retry_policy'] != POLICY:
                        raise ContractError('unknown_qwen_output_retry_policy')
                    outcome, retry_audit = generate(runner, candidate_id=p['candidate_id'],image_path=image_path(p),prompt=prompt)
                else:
                    outcome=runner.generate_one(candidate_id=p['candidate_id'],image_path=image_path(p),prompt=prompt)
            finally:generation_event('inference_end')
            generation_ms=(time.perf_counter()-generation_start)*1000
            validation_start=time.perf_counter()
            from .qwen_structural_normalization import repair
            outcome,normalization_audit=repair(outcome,m,cfg['max_tokens'])
            schema_audit={**(schema_audit or {}),'structural_normalization':normalization_audit}
            if cfg.get('_inference_event_log'):
                from .video_telemetry import append
                append(cfg['_inference_event_log'],dict(kind='QWEN_TIMING',timestamp=time.time(),candidate_id=p['candidate_id'],
                    input_prepare_ms='UNKNOWN',image_decode_ms='UNKNOWN',prefill_ms='UNKNOWN',
                    generation_ms=generation_ms,decode_parse_ms='UNKNOWN',validation_ms=(time.perf_counter()-validation_start)*1000,
                    persist_ms='UNKNOWN',total_ms=(time.perf_counter()-timing_start)*1000,scope='handler_before_persistence'))
            if outcome.result_status!='success':
                outcome_audit=asdict(outcome)
                if (schema_audit or {}).get('group_correction_applied'):
                    outcome_audit['_schema_group_correction_applied']=True
                raise ContractError('qwen_requires_review:'+outcome.result_status+':'+json.dumps({'candidate_id':p['candidate_id'],'outcome':outcome_audit,'schema_retry_audit':schema_audit},ensure_ascii=False))
            from qwenvl_raw_media_v3 import is_raw_media_prompt, parse_fields, CONTRACT_REVISION
            sections = {key + '：': value for key, value in parse_fields(outcome.clean_text).items()}
            result = {'contract':'production-qwen-sections-v1','text':outcome.clean_text,'sections':sections,'raw':asdict(outcome)}
            if is_raw_media_prompt(prompt):
                result['qwen_schema_version'] = 'raw_media_v3'
                result['qwen_contract_revision'] = CONTRACT_REVISION
            if retry_audit is not None: result['output_retry_audit'] = retry_audit
            if schema_audit is not None: result['schema_retry_audit'] = schema_audit
            return result
        return qwen
    if stage=='embedding':
        m=module(stage);m.block_worker_network();model=m.LocalSentenceTransformerAdapter(Path(cfg['model_path']),cfg['device']);model.load_once()
        return lambda p:{'vector':model.encode(p['text'])}
    if stage in ('audio_extract','vad'):
        m=module(stage)
        if stage=='audio_extract':
            def extract(p):
                probe=subprocess.run([cfg['ffprobe'],'-v','error','-select_streams','a','-show_entries','stream=index','-of','json',p['path']],capture_output=True,text=True,check=True)
                if not json.loads(probe.stdout)['streams']:return {'status':'NO_AUDIO'}
                import os,tempfile
                root=Path(cfg['artifact_root']);root.mkdir(parents=True,exist_ok=True)
                fd,tmp=tempfile.mkstemp(dir=root,suffix='.wav');os.close(fd);temporary=Path(tmp)
                try:
                    m.extract_pcm_audio(cfg['ffmpeg'],Path(p['path']),temporary,sample_rate=16000,tolerate_corrupt_source=False)
                    m.read_pcm16_mono(temporary);digest=sha_file(temporary);out=root/(digest+'.wav')
                    if not out.exists():
                        try:os.link(temporary,out)
                        except FileExistsError:pass
                    if sha_file(out)!=digest:raise ContractError('audio_artifact_integrity_conflict')
                    return {'status':'READY','path':str(out),'sha':digest,'sample_rate':16000}
                finally:temporary.unlink(missing_ok=True)
            return extract
        silero=m.load_local_silero(Path(cfg['package_root']));model=silero.load_silero_vad()
        def vad(p):
            audio,rate,_=m.read_pcm16_mono(Path(p['path']))
            segments=silero.get_speech_timestamps(audio,model,sampling_rate=rate,**cfg.get('parameters',{}))
            return {'segments':[{'start_ms':round(x['start']*1000/rate),'end_ms':round(x['end']*1000/rate)} for x in segments]}
        return vad
    if stage=='whisper':
        from mlx_whisper.transcribe import transcribe
        from .chinese_text import POLICY as TEXT_POLICY, whisper_parameters, simplified_chinese
        policy = cfg.get('text_normalization')
        if policy not in (None, TEXT_POLICY):raise ContractError('unknown_transcript_text_policy')
        parameters = whisper_parameters(cfg.get('parameters', {})) if policy else dict(cfg.get('parameters', {}))
        def whisper(p):
            result=transcribe(p['path'],path_or_hf_repo=cfg['model_path'],clip_timestamps='0' if p.get('_asr_local_zero') else [p['start_ms']/1000,p['end_ms']/1000],**parameters)
            if not policy:return {'text':result['text'],'language':result.get('language'),'raw':result}
            return {'text':simplified_chinese(result['text']),'language':result.get('language'),'raw':result,
                    'text_normalization': TEXT_POLICY, 'decoding_parameters': parameters}
        return whisper
    raise ContractError('unsupported_stage:'+stage)
