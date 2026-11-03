"""Owned per-attempt PCM conversion; no original mutation or database writes."""
import hashlib,json,os,shutil,subprocess,tempfile,threading,time,wave
from pathlib import Path
from functools import lru_cache

class PCMConversionError(RuntimeError):pass

def validate_pcm(path):
    with wave.open(str(path),'rb') as f:
        if (f.getnchannels(),f.getsampwidth(),f.getframerate(),f.getcomptype())!=(1,2,16000,'NONE') or f.getnframes()<=0:
            raise ValueError('PCM_FORMAT_OR_EMPTY')
        frames=f.getnframes();f.setpos(frames-1)
        if len(f.readframes(1))!=2:raise ValueError('PCM_TRUNCATED')
    return frames

@lru_cache(maxsize=4)
def ffmpeg_version(executable):
    try:
        p=subprocess.run([executable,'-version'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
        return p.stdout[:2048].decode('utf-8','replace').splitlines()[0]
    except Exception as e:return 'UNKNOWN:'+type(e).__name__

def convert(payload,root,stage,context=None,*,executable='ffmpeg',timeout=300):
    root=Path(root);root.mkdir(exist_ok=True)
    identity=hashlib.sha256((payload['path']+payload['sha']).encode()).hexdigest();target=root/(identity+'.wav')
    owned=Path(tempfile.mkdtemp(prefix='.pcm-attempt-',dir=root));partial=owned/'audio.wav'
    executable=shutil.which(executable) or executable
    command=[executable,'-nostdin','-v','error','-n','-i',payload['path'],'-ac','1','-ar','16000','-c:a','pcm_s16le',str(partial)]
    started=time.time();mono=time.monotonic();tail=bytearray();errors=[];process=None;phase='PCM_CONVERT';exit_code=None
    audit={k:(context or {}).get(k,'UNKNOWN') for k in ('source_id','revision','job_id','attempt')}
    audit.update(stage=stage,input_identity={k:payload.get(k) for k in ('path','sha','_file_sha','representation')},command=command,ffmpeg_version=ffmpeg_version(executable),started_at=started)
    try:
        if target.exists():
            phase="PCM_EXISTING_VALIDATE";validate_pcm(target);return target
        process=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        def drain():
            try:
                while data:=process.stderr.read(4096):
                    tail.extend(data)
                    if len(tail)>8192:del tail[:-8192]
            except Exception as e:errors.append(type(e).__name__)
        reader=threading.Thread(target=drain,daemon=True);reader.start()
        try:exit_code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            exit_code=process.returncode;raise TimeoutError('PCM_CONVERT_TIMEOUT')
        finally:
            reader.join(timeout=5)
            if not reader.is_alive():process.stderr.close()
        if reader.is_alive() or errors:raise RuntimeError('PCM_STDERR_DRAIN_INCOMPLETE')
        if exit_code:raise subprocess.CalledProcessError(exit_code,command)
        phase='PCM_VALIDATE';validate_pcm(partial)
        with partial.open('rb') as f:os.fsync(f.fileno())
        phase='PCM_PUBLISH'
        try:os.link(partial,target) # no replace of a concurrent successful result
        except FileExistsError:validate_pcm(target)
        fd=os.open(root,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return target
    except Exception as error:
        audit.update(failed_phase=phase,ended_at=time.time(),elapsed_seconds=time.monotonic()-mono,
                     exit_code=exit_code,error_type=type(error).__name__,error=str(error)[:2048],stderr_tail=tail.decode('utf-8','replace'))
        evidence=root/(identity+'.'+owned.name+'.error.json')
        raw=json.dumps(audit,ensure_ascii=False,sort_keys=True)
        try:
            with evidence.open('x') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        except Exception as save_error:
            raise PCMConversionError('PCM_ERROR_EVIDENCE_WRITE_FAILED:'+type(save_error).__name__+':'+raw) from error
        raise PCMConversionError('PCM_CONVERSION_FAILED:'+str(evidence)+':'+raw) from error
    finally:
        # Only this call owns this newly created directory. Never remove legacy
        # partials, other attempt directories, the target, or the input.
        if partial.exists():partial.unlink()
        owned.rmdir()
