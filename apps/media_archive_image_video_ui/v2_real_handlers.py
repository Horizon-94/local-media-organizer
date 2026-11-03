"""Production handler resolution for the V2 product entrypoint.

The validated Phase-1 model environments are intentionally separate.  This
module keeps that deployment contract explicit while presenting the existing
V2 ``handler_factory`` interface to the product driver.  Each expensive
stage owns one resident subprocess; model code is never imported by the
product process itself.
"""
from __future__ import annotations

import codecs
import io
import json
import os
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

from .v2_documents import validate_result
from .v2_runtime import validate_evidence


ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = Path(os.environ.get(
    "MEDIA_ARCHIVE_MODEL_ROOT",
    Path(__file__).resolve().parents[4] / "model",
)).expanduser().resolve()
ENV_ROOT = Path(os.environ.get(
    "MEDIA_ARCHIVE_ENV_ROOT",
    Path(__file__).resolve().parents[3] / "envs",
)).expanduser().resolve()


def _python(name: str) -> str:
    path = ENV_ROOT / name / "bin" / "python"
    if not path.is_file():
        raise RuntimeError(f"real_handler_python_missing:{path}")
    return str(path)


def default_stage_configs(workspace: str | Path) -> dict[str, dict]:
    workspace = Path(workspace).expanduser().resolve()
    configs = {
        "yoloe": {
            "python_path": _python("media-archive-v06-yolo"),
            "model_path": str(MODEL_ROOT / "yoloe26-l-seg/weights/yoloe26-l-seg.pt"),
            "mobileclip_path": str(MODEL_ROOT / "yoloe26-l-seg/mobileclip2_b.ts"),
            "registry": str(ROOT / "configs/yoloe_keyword_registry_default_v1.json"),
            "allowed_model_root": str(MODEL_ROOT), "include_b": True,
            "include_ocr_trigger": True, "device": "cpu", "imgsz": 640,
            "confidence": 0.25,
        },
        "openclip": {
            "python_path": _python("media-archive-v06-visual"),
            "model_path": str(MODEL_ROOT / "openclip-vit-b-32-laion2b-s34b-b79k/open_clip_model.safetensors"),
            "model_name": "ViT-B-32", "allowed_model_root": str(MODEL_ROOT),
            "device": "cpu",
        },
        "ocr": {
            "python_path": _python("media-archive-v06-ocr"),
            "text_detection_model_dir": str(MODEL_ROOT / "ocr/PP-OCRv6_medium_det"),
            "text_recognition_model_dir": str(MODEL_ROOT / "ocr/PP-OCRv6_medium_rec"),
            "allowed_model_root": str(MODEL_ROOT), "paddlex_cache_root": str(workspace / "paddlex_cache"),
        },
        "embedding": {
            "python_path": _python("media-archive-embedding"),
            "model_path": str(MODEL_ROOT / "Qwen3-Embedding-0.6B"),
            "allowed_model_root": str(MODEL_ROOT), "device": "cpu",
        },
        "qwen": {
            "python_path": _python("qwen-vl"),
            "model_path": str(MODEL_ROOT / "Qwen3.5-9B-4bit"),
            "prompt_path": str(ROOT / "configs/qwenvl_prompt_raw_media_v3_384.txt"),
            "schema_retry_policy":"raw_media_v3_schema_reask_uncertainty_once_v2",
            "allowed_model_root": str(MODEL_ROOT), "max_tokens": 384,
            "temperature": 0.0, "top_p": 1.0,
            "repetition_penalty": 1.1, "repetition_context_size": 64,
            # Qwen is intentionally conservative because it is the slowest
            # real stage.  The deadline bounds a stalled protocol request;
            # it does not change model or scheduler concurrency policy.
            "response_timeout_seconds": float(
                os.environ.get("MEDIA_ARCHIVE_QWEN_RESPONSE_TIMEOUT_SECONDS", "300")
            ),
        },
        "whisper": {
            "python_path": _python("whisper"),
            "model_path": str(MODEL_ROOT / "whisper-large-v3-turbo"),
            "allowed_model_root": str(MODEL_ROOT), "parameters": {"language": "zh", "task": "transcribe", "initial_prompt": "以下是中文语音的简体中文转写。保留英文原文、数字和标点。"}, "text_normalization": "zh-hans-transcription-v1",
        },
        "vad": {
            "python_path": _python("media-archive-v06-vad"),
            "package_root": str(MODEL_ROOT / "silero-vad"),
            "allowed_model_root": str(MODEL_ROOT), "parameters": {},
        },
        "audio_extract": {
            "python_path": _python("media-archive-v06-visual"),
            "ffmpeg": os.environ.get("MEDIA_ARCHIVE_FFMPEG", "/opt/homebrew/bin/ffmpeg"),
            "ffprobe": os.environ.get("MEDIA_ARCHIVE_FFPROBE", "/opt/homebrew/bin/ffprobe"),
            "artifact_root": str(workspace / "audio"),
        },
    }
    person = json.loads(
        (ROOT / "configs/stop03_1c_person_reid_db_v1.json").read_text(encoding="utf-8")
    )
    person.update({
        "python_path": _python("media-archive-v06-yolo"),
        "model_dir": str(MODEL_ROOT / "insightface/buffalo_l"),
        "allowed_model_root": str(MODEL_ROOT),
    })
    configs["insightface"] = person
    return configs


class _StderrCapture:
    """Drain worker diagnostics independently of requests, with bounded RAM."""
    LIMIT = 64 * 1024

    def __init__(self, stream, name):
        self.stream = stream
        self.tail = bytearray()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._drain, name=name, daemon=True)
        self.thread.start()

    def _drain(self):
        try:
            fd = self.stream.fileno()
            while not self.stop.is_set():
                readable, _, _ = select.select([fd], [], [], 0.1)
                if not readable:
                    continue
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    return
                with self.lock:
                    self.tail.extend(chunk)
                    if len(self.tail) > self.LIMIT:
                        del self.tail[:-self.LIMIT]
        except (OSError, ValueError) as exc:
            self.error = type(exc).__name__ + ':' + str(exc)

    def text(self):
        with self.lock:
            value = bytes(self.tail)
        text = value.decode('utf-8', errors='replace')
        return text + ('\n[stderr_capture_error:' + self.error + ']' if self.error else '')

    def finish(self):
        # After process exit, allow EOF/final diagnostic bytes to be collected.
        # A descendant holding the pipe open must not make shutdown hang.
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            self.stop.set()
            self.thread.join(timeout=1)
        if not self.thread.is_alive():
            self.stream.close()


class SubprocessModelHandler:
    def __init__(self, stage: str, config: dict, *, resource_monitor=None):
        self.stage = stage
        self.config = dict(config)
        self.event_log = self.config.pop("_event_log", None)
        self.response_timeout_seconds = float(self.config.pop("response_timeout_seconds", 300.0))
        if self.response_timeout_seconds <= 0:
            raise ValueError("response_timeout_seconds_must_be_positive")
        self._lock = threading.Lock()
        self.has_completed_result = False
        self.resource_monitor = resource_monitor
        if resource_monitor is not None:
            self.config['_worker_ready_ack'] = True
        command = [self.config.pop("python_path"), "-m",
                   "apps.media_archive_image_video_ui.v2_real_model_worker",
                   "--stage", stage, "--config-json", json.dumps(self.config)]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        self.process = subprocess.Popen(command, cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, bufsize=0, start_new_session=True)
        self._stderr_capture = _StderrCapture(
            self.process.stderr, f'model-stderr-{stage}-{self.process.pid}')
        if resource_monitor is not None:
            self.resource_worker_id = f'{stage}:{self.process.pid}'
            resource_monitor.register(stage, self.resource_worker_id, self.process.pid)
            try:
                ready = json.loads(self._read_response({}, time.time()))
                if ready != {'ok': True, 'worker_state': 'READY'}:
                    raise RuntimeError('worker_ready_protocol_invalid')
                resource_monitor.transition(self.resource_worker_id, 'READY')
            except Exception:
                self._terminate_owned_worker()
                resource_monitor.transition(self.resource_worker_id, 'CLOSED')
                raise

    def is_healthy(self) -> bool:
        """Whether this owned worker can safely be reused for a new job."""
        return (self.process.poll() is None and
                self.process.stdin is not None and not self.process.stdin.closed and
                self.process.stdout is not None and not self.process.stdout.closed)

    def __call__(self, payload: dict) -> dict:
        with self._lock:
            monitor = getattr(self, 'resource_monitor', None)
            if monitor is not None:
                monitor.transition(self.resource_worker_id, 'INFERENCE')
            try:
                return self._call_locked(payload)
            finally:
                if monitor is not None:
                    monitor.transition(self.resource_worker_id,
                        'IDLE' if self.process.poll() is None else 'CLOSED')

    def _call_locked(self, payload: dict) -> dict:
            started = time.time()
            self._log_event("start", started, payload)
            if self.process.poll() is not None:
                error = self._stderr_text()
                raise RuntimeError(f"real_handler_process_exited:{self.stage}:{error[-500:]}")
            assert self.process.stdin is not None and self.process.stdout is not None
            request = json.dumps(payload, ensure_ascii=False) + "\n"
            try:
                self.process.stdin.write(request)
            except TypeError:
                self.process.stdin.write(request.encode("utf-8"))
            self.process.stdin.flush()
            line = self._read_response(payload, started)
            if not line:
                error = self._stderr_text()
                raise RuntimeError(f"real_handler_no_response:{self.stage}:{error[-500:]}")
            result = json.loads(line)
            if not result.get("ok"):
                raise RuntimeError(result.get("error", f"real_handler_failed:{self.stage}"))
            self._log_event("finish", time.time(), payload)
            self.has_completed_result = True
            return result["result"]

    def batch(self, payloads):
        if self.stage!='openclip' or not isinstance(payloads,list) or not 1<=len(payloads)<=8:
            raise ValueError('true_batch_not_supported_or_invalid_size:'+self.stage)
        results=self({'request_kind':'v2_true_batch','items':payloads})
        if not isinstance(results,list) or len(results)!=len(payloads):
            raise RuntimeError('true_batch_result_cardinality_mismatch')
        return results

    def _read_response(self, payload: dict, started: float) -> str:
        """Read one newline-delimited response with a real byte deadline.

        ``readline`` is deliberately avoided after readiness: a child can
        write a partial frame and keep the pipe open, which would otherwise
        block past ``response_timeout_seconds``.
        """
        assert self.process.stdout is not None
        deadline = time.monotonic() + self.response_timeout_seconds
        stream = self.process.stdout
        raw_stream = getattr(stream, "buffer", stream)
        decoder = codecs.getincrementaldecoder("utf-8")()
        frame = bytearray()
        while True:
            if self.process.poll() is not None:
                error = self._stderr_text()
                raise RuntimeError(f"real_handler_process_exited:{self.stage}:{error[-500:]}")
            capture = getattr(self, '_stderr_capture', None)
            if capture is not None and capture.error:
                self._terminate_owned_worker()
                raise RuntimeError(f'real_handler_stderr_drain_failed:{self.stage}:{capture.error}')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                elapsed = time.time() - started
                self._log_event("timeout", time.time(), payload)
                self._terminate_owned_worker()
                raise TimeoutError(
                    f"real_handler_response_timeout:{self.stage}:"
                    f"elapsed={elapsed:.3f}:pid={self.process.pid}"
                )
            try:
                readable, _, _ = select.select([self.process.stdout], [], [], min(remaining, 0.25))
            except (OSError, ValueError) as exc:
                self._terminate_owned_worker()
                raise RuntimeError(f"real_handler_pipe_error:{self.stage}:{exc}") from exc
            if readable:
                try:
                    # ``select`` watches the pipe descriptor.  Reading through
                    # TextIOWrapper.buffer can disagree with that readiness
                    # after the wrapper has buffered or prefetched bytes, so
                    # use the ready descriptor directly for real subprocesses.
                    fileno = getattr(stream, "fileno", None)
                    fd = None
                    if callable(fileno):
                        try:
                            fd = fileno()
                        except (OSError, ValueError, io.UnsupportedOperation):
                            fd = None
                    if fd is not None:
                        chunk = os.read(fd, 4096)
                    else:
                        reader = getattr(raw_stream, "read1", None)
                        chunk = reader(4096) if reader is not None else raw_stream.read(4096)
                except (OSError, ValueError) as exc:
                    self._terminate_owned_worker()
                    raise RuntimeError(f"real_handler_pipe_error:{self.stage}:{exc}") from exc
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if not chunk:
                    error = self._stderr_text()
                    raise RuntimeError(f"real_handler_incomplete_frame:{self.stage}:{error[-500:]}")
                frame.extend(chunk)
                newline = frame.find(b"\n")
                if newline >= 0:
                    line = bytes(frame[:newline + 1])
                    return decoder.decode(line, final=True)

    def _stderr_text(self) -> str:
        capture = getattr(self, '_stderr_capture', None)
        if capture is None:
            return ''  # Injected fixture handlers have no real pipe reader.
        if self.process.poll() is not None:
            capture.finish()
        return capture.text()

    def _log_event(self, phase: str, timestamp: float, payload: dict) -> None:
        if self.stage=="whisper":
            from .build151_whisper_feed import physical
            physical(self.event_log,phase,timestamp,payload,self.process.pid)
        if not self.event_log:
            return
        record = {"stage": self.stage, "phase": phase, "timestamp": timestamp, "worker_pid": self.process.pid}
        for key in ("source_id", "item_id", "artifact_id", "visual_unit_id"):
            if key in payload:
                record[key] = payload[key]
        with open(self.event_log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def close(self):
        try:
            self._close_owned_worker()
        finally:
            if self.process.poll() is not None:
                self._finish_stderr_capture()
                monitor = getattr(self, 'resource_monitor', None)
                if monitor is not None:
                    monitor.transition(self.resource_worker_id, 'CLOSED')

    def _finish_stderr_capture(self):
        capture = getattr(self, '_stderr_capture', None)
        if capture is not None:
            capture.finish()

    def _close_owned_worker(self):
        if self.stage in ('ocr','qwen') or getattr(self,'resource_monitor',None) is not None:
            with self._lock:
                previous_code=self.process.poll()
                if previous_code is not None and previous_code!=0:
                    raise RuntimeError(self.stage.upper()+'_WORKER_ALREADY_FAILED:'+str(previous_code))
                if previous_code is None:
                    assert self.process.stdin is not None
                    self._log_event('shutdown_request',time.time(),{})
                    self.process.stdin.write(b'{"request_kind":"v2_worker_shutdown"}\n')
                    self.process.stdin.flush()
                    try:
                        code=self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._terminate_owned_worker()
                        raise RuntimeError(self.stage.upper()+'_GRACEFUL_SHUTDOWN_TIMEOUT:'+str(self.process.pid))
                    if code!=0:
                        raise RuntimeError(self.stage.upper()+'_GRACEFUL_SHUTDOWN_FAILED:'+str(code))
                    assert self.process.stdout is not None
                    acknowledgement=self.process.stdout.readline()
                    try:
                        acknowledged=json.loads(acknowledgement)=={'ok':True,'shutdown':'ack'}
                    except (ValueError,TypeError):
                        acknowledged=False
                    if not acknowledged:
                        raise RuntimeError(self.stage.upper()+'_GRACEFUL_SHUTDOWN_ACK_MISSING')
                    self._log_event('shutdown_complete',time.time(),{})
            return
        self._terminate_owned_worker()

    def _terminate_owned_worker(self):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise RuntimeError('GRACEFUL_MODEL_WORKER_STOP_TIMEOUT:'+str(self.process.pid))
        self._finish_stderr_capture()


def build_handler_factory(workspace: str | Path, configs: dict | None = None, *, runtime_workers=None):
    workspace = Path(workspace).expanduser().resolve()
    # Fully materialized product tasks pin their portable runtime configs.
    # Do not resolve host development environments before using those pins.
    required = {'yoloe', 'openclip', 'ocr', 'embedding', 'qwen',
                'whisper', 'vad', 'audio_extract', 'insightface'}
    authoritative = isinstance(configs, dict) and required.issubset(configs)
    if authoritative:
        if any(not isinstance(configs[stage], dict) or not configs[stage]
               for stage in required):
            raise ValueError('real_handler_config_invalid')
        specs = {stage: dict(configs[stage]) for stage in required}
    else:
        specs = default_stage_configs(workspace)
    event_log = workspace / "real_handler_events.jsonl"
    for spec in specs.values():
        spec["_event_log"] = str(event_log)
    if configs:
        for stage, override in configs.items():
            if stage in specs:
                specs[stage].update(override)
    if 'qwen' in specs:
        specs['qwen']['_inference_event_log'] = str(workspace/'qwen_inference_events.jsonl')
    handlers = {}
    # Historical single-worker configs can be shared by multiple queued
    # futures. Membership alone loses ownership after the first release.
    leased = {}

    def acquire(handler):
        key=id(handler)
        leased[key]=leased.get(key,0)+1

    def _lease_lightweight(stage, creator):
        handler = handlers.get(stage)
        if handler is None:
            handler = creator()
            handlers[stage] = handler
        acquire(handler)
        return handler

    def factory(stage, _vectors, _task):
        if stage in ("dhash", "quality", "geometry"):
            from .v2_model_workers import create_handler
            handler = _lease_lightweight(stage, lambda: create_handler(stage, {}))
            return handler, lambda result, stage=stage: validate_evidence(stage, result)
        if stage in ("audio_extract", "vad", "whisper") and stage not in specs:
            from .v2_model_workers import create_handler
            handler = _lease_lightweight(stage, lambda: create_handler(stage, {}))
            return handler, lambda result, stage=stage: validate_result(stage, result)
        if stage not in specs:
            raise RuntimeError(f"real_handler_config_missing:{stage}")
        pool_limit = (runtime_workers or {}).get(stage,specs[stage].get('resident_worker_limit', 2 if stage == 'ocr' else 1))
        if type(pool_limit) is not int or pool_limit < 1:
            raise ValueError('invalid_resident_worker_limit:'+stage)
        if stage == "ocr" or pool_limit > 1:
            pool = handlers.setdefault(stage, [])
            if not isinstance(pool, list):
                pool = [pool]
                handlers[stage] = pool
            pool[:] = [item for item in pool if item.is_healthy() or id(item) in leased]
            handler = next((item for item in pool if id(item) not in leased and item.is_healthy()), None)
            if handler is None:
                if len(pool) >= pool_limit:
                    raise RuntimeError('resident_worker_pool_capacity_exhausted:'+stage)
                handler = SubprocessModelHandler(stage, specs[stage])
                pool.append(handler)
        else:
            handler = handlers.get(stage)
            if handler is not None and hasattr(handler, "is_healthy") and not handler.is_healthy():
                handlers.pop(stage, None)
                try:
                    handler.close()
                except Exception:
                    pass
                handler = None
            if handler is None:
                handler = SubprocessModelHandler(stage, specs[stage])
                handlers[stage] = handler
        acquire(handler)
        validator = validate_result if stage in ("qwen", "ocr", "embedding", "whisper", "audio_extract", "vad") else validate_evidence
        if stage == "qwen":
            def qwen_contract_handler(payload):
                result = handler(payload)
                raw = result.get("raw") if isinstance(result, dict) else None
                if isinstance(raw, dict) and raw.get("truncation_status") == "not_truncated":
                    # The local adapter's runtime wording maps to the frozen
                    # V2 projection contract's successful terminal value.
                    raw = dict(raw)
                    raw["truncation_status"] = "complete"
                    result = {**result, "raw": raw}
                return result
            qwen_contract_handler._resident_handler = handler
            return qwen_contract_handler, lambda result: validator(stage, result)
        return handler, lambda result, stage=stage: validator(stage, result)

    factory.handlers = handlers
    def close_all():
        errors = []
        values = []
        for value in handlers.values():
            values.extend(value if isinstance(value, list) else [value])
        for handler in values:
            if hasattr(handler, "close"):
                try:
                    handler.close()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]
    factory.close = close_all

    def release(handler):
        handler=getattr(handler,'_resident_handler',handler)
        key=id(handler)
        if leased.get(key,0)>1:leased[key]-=1
        else:
            leased.pop(key,None)
            handler._idle_since=time.monotonic()
    factory.release = release

    def release_idle(stage, minimum_idle_seconds=0, keep=0):
        value = handlers.get(stage)
        values = value if isinstance(value, list) else [value] if value is not None else []
        now=time.monotonic()
        retiring=[handler for handler in values if id(handler) not in leased and
                  now-getattr(handler,'_idle_since',now)>=minimum_idle_seconds]
        retiring=retiring[:max(0,len(values)-keep)]
        keeping=[handler for handler in values if handler not in retiring]
        if keeping:handlers[stage]=keeping if isinstance(value,list) else keeping[0]
        else:handlers.pop(stage,None)
        for handler in retiring:
            if hasattr(handler, "close"):
                handler.close()
        return len(retiring)
    factory.release_idle = release_idle
    factory.release_idle_due = lambda stage,keep=0,upstream_pending=False: release_idle(stage,120 if upstream_pending else 30,keep)
    factory.bounded_upstream_idle = True
    def trim_idle(stage, selected_limit):
        """Retire excess residents only after their owning job released them."""
        value=handlers.get(stage)
        values=value if isinstance(value,list) else [value] if value is not None else []
        excess=max(0,len(values)-selected_limit)
        retiring=[handler for handler in reversed(values) if id(handler) not in leased][:excess]
        keeping=[handler for handler in values if handler not in retiring]
        if keeping:handlers[stage]=keeping if isinstance(value,list) else keeping[0]
        else:handlers.pop(stage,None)
        for handler in retiring:
            if hasattr(handler,'close'):handler.close()
        return len(retiring)
    factory.trim_idle = trim_idle
    def resident_ready(stage):
        value=handlers.get(stage)
        values=value if isinstance(value,list) else [value] if value is not None else []
        return any(id(handler) not in leased and getattr(handler,'has_completed_result',False) and
                   handler.is_healthy() for handler in values)
    factory.resident_ready = resident_ready
    def retire_idle_models(protected_stages=(),maximum_workers=None):
        retired={};remaining=maximum_workers
        for stage in list(handlers):
            if stage in protected_stages:continue
            value=handlers[stage];values=value if isinstance(value,list) else [value]
            if remaining is not None and not any(hasattr(h,'process') for h in values):continue
            count=release_idle(stage,keep=max(0,len(values)-remaining) if remaining is not None else 0)
            if count:retired[stage]=count
            if remaining is not None:
                remaining-=count
                if remaining<=0:break
        return retired
    factory.retire_idle_models = retire_idle_models
    return factory
