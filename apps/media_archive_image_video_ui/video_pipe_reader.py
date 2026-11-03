"""One input, bounded pipes, parent-owned strict media spool writes.

FFmpeg never writes derived files itself, so a byte limit is checked BEFORE
each parent write (not after a polling overshoot). Original is opened only by
the demuxer; stream-table probing is metadata-only. No model imports here.
"""
from .stop_provenance import child_run,child_spawn
import os
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from .video_stream_reader import probe,reserved_bytes


class DecodeFailure(RuntimeError):
    """Only positively classified source/decoder failures cross this boundary."""
    def __init__(self, evidence):
        self.evidence=evidence
        super().__init__('visual_decode_failed:'+evidence['classification'])


def decode_classification(code, stderr, mode):
    # Exit 69 alone is never sufficient. Encoder, disk, pipe, executable and
    # unknown failures remain fatal. VT init may internally fall back at FFmpeg.
    if code not in (1,69):return None
    text=stderr.lower()
    hw=('vt decoder cb: output image buffer is null' in text or
        'hardware accelerator failed to decode picture' in text)
    if mode=='videotoolbox' and hw:
        return 'RECOVERABLE_HW_DECODE_FAILURE'
    source_markers=('invalid nal unit','error splitting the input into nal units',
        'invalid data found when processing input','moov atom not found',
        'decode error rate','error while decoding stream','decoding error:')
    if any(marker in text for marker in source_markers):
        return 'SOURCE_LOCAL_PERMANENT_DECODE_FAILURE' if mode=='software' else 'RECOVERABLE_TRANSIENT_DECODE_FAILURE'
    return None


class SpoolWriter:
    def __init__(self,root,ceiling,free_floor):
        self.root=Path(root).resolve();self.ceiling=ceiling;self.free_floor=free_floor
        self.bytes=0;self.lock=threading.Lock()
    def write(self,path,data,mode='ab'):
        path=Path(path)
        if not path.resolve().is_relative_to(self.root):raise ValueError('spool_path_not_owned')
        with self.lock:
            if self.bytes+len(data)>self.ceiling:raise ValueError('SPOOL_HARD_BYTE_CEILING')
            if shutil.disk_usage(self.root).free-len(data)<self.free_floor:raise ValueError('INTERNAL_DISK_SPACE_LOW')
            with path.open(mode) as f:
                f.write(data);f.flush()
            self.bytes+=len(data)


def pipe_command(source,audio_fd,has_audio,ffmpeg,short_time=None):
    # Square sources also obey frozen long-edge1280; two simultaneous -2
    # dimensions otherwise preserve the original oversized geometry.
    scale="scale='if(gte(iw,ih),1280,-2)':'if(gt(ih,iw),1280,-2)'"
    vf='fps=1/3.0:start_time=2.0,'+scale if short_time is None else f'select=gte(t\\,{short_time}),'+scale
    args=[ffmpeg,'-nostdin','-hide_banner','-i',str(source),'-map','0:v:0','-an','-sn','-dn','-vf',vf,'-q:v','3']
    if short_time is not None:args+=['-frames:v','1','-fps_mode','vfr']
    args+=['-f','image2pipe','-c:v','mjpeg','-flush_packets','1','pipe:1']
    if has_audio:args+=['-map','0:a:0','-vn','-sn','-dn','-ac','1','-ar','16000','-c:a','flac','-compression_level','5','-f','flac',f'pipe:{audio_fd}']
    return args


def run(source,output,*,on_frame,stop,reservation,ffmpeg='ffmpeg',ffprobe='ffprobe',probe_info=None,on_audio_ready=None,variant='C',decode_mode='videotoolbox'):
    if variant!='C':raise ValueError('unknown_video_producer_variant')
    source=Path(source).resolve(strict=True);output=Path(output).resolve()
    if source.parent==output or source.parent in output.parents:raise ValueError('output_in_original_media_directory')
    if output.exists():raise ValueError('new_reader_attempt_directory_required')
    info=probe_info or probe(source,ffprobe,stop=stop);duration=info['duration_seconds']
    if duration<.5:raise ValueError('below_frozen_short_video_minimum')
    short=(1.5 if duration>=1.5 else duration/2) if duration<=2 else None
    # fps rounds its start/end on a 3-second time base: a valid single
    # sampling window can emit zero frames before 4.5s. Select the frozen
    # 2-second timestamp explicitly for that one-frame window. No policy,
    # source revision, model parameters, or existing frame identity changes.
    if 2<duration<4.5:short=2.0
    ceiling=reserved_bytes(duration)
    token=reservation.acquire(ceiling,stop)
    if token is None:return {'status':'STOPPED_BEFORE_READ','source_reads':0}
    output.mkdir(parents=True)
    reservation.bind_directory(token,output)
    writer=SpoolWriter(output,ceiling,reservation.disk_free_floor)
    read_fd,write_fd=os.pipe();errors=[];frames=[];tail=bytearray()
    before=source.stat();start=time.monotonic()
    args=pipe_command(source,write_fd,info['has_audio'],ffmpeg,short)
    audio_args=None;audio_process=None
    if os.uname().sysname=='Darwin' and decode_mode=='videotoolbox':args[3:3]=['-hwaccel','videotoolbox']
    if info['has_audio']:
        split=args.index('-map',args.index('pipe:1'))
        native=['-map','0:a:0','-vn','-sn','-dn','-c:a','copy','-f','matroska',f'pipe:{write_fd}']
        args=args[:split]
        audio_args=[ffmpeg,'-nostdin','-hide_banner','-i',str(source),*native]
    audio_name='source-audio.mka'
    process=None;audio_ready_at=[None]
    try:
        if audio_args:
            audio_process=child_spawn(stop,audio_args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,pass_fds=(write_fd,),start_new_session=True)
            (output/'audio-ffmpeg.pid').write_text(str(audio_process.pid))
        process=child_spawn(stop,args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=() if audio_args else (write_fd,),start_new_session=True)
        os.close(write_fd);write_fd=None
        (output/'ffmpeg.pid').write_text(str(process.pid))
        (output/'decoder-command.json').write_text(json.dumps(dict(command=args,decode_mode=decode_mode,pid=process.pid,at=time.time())))
        def audio():
            try:
                with os.fdopen(read_fd,'rb') as pipe:
                    while data:=pipe.read(65536):writer.write(output/(audio_name+'.partial'),data)
                if info['has_audio'] and not stop.is_set():
                    if audio_process and audio_process.wait(timeout=10)!=0:raise RuntimeError('audio_ffmpeg_failed')
                    path=output/audio_name
                    (output/(audio_name+'.partial')).rename(path)
                    checked=child_run(stop,[ffprobe,'-v','error','-select_streams','a:0','-show_entries','stream=codec_name,sample_rate,channels','-of','json',str(path)],capture_output=True,text=True,check=True,timeout=15)
                    import json
                    streams=json.loads(checked.stdout)['streams']
                    if not streams:raise ValueError('audio_artifact_not_closed_or_valid')
                    audio_ready_at[0]=time.time()
                    if on_audio_ready:on_audio_ready(path,streams[0])
            except Exception as e:errors.append(e)
        def stderr():
            while data:=process.stderr.read(4096):
                tail.extend(data)
                if len(tail)>65536:del tail[:-65536]
        def images():
            try:
                buffer=bytearray()
                while data:=process.stdout.read1(65536):
                    buffer.extend(data)
                    while True:
                        end=buffer.find(b'\xff\xd9')
                        if end<0:break
                        jpeg=bytes(buffer[:end+2]);del buffer[:end+2]
                        if not jpeg.startswith(b'\xff\xd8'):raise ValueError('invalid_mjpeg_frame')
                        path=output/f'frame_{len(frames)+1:06d}.jpg'
                        writer.write(path,jpeg,'xb')
                        timestamp=round(short*1000) if short is not None else 2000+len(frames)*3000
                        on_frame(path,timestamp);frames.append(str(path))
                    if len(buffer)>1280*1280*3+65536:raise ValueError('encoded_frame_size_exceeds_frozen_bound')
                if buffer and not stop.is_set():raise ValueError('incomplete_mjpeg_frame')
            except Exception as e:errors.append(e)
        threads=[threading.Thread(target=call,daemon=True) for call in (audio,stderr,images)]
        for thread in threads:thread.start()
        while process.poll() is None:
            if stop.is_set() or errors:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                break
            stop.wait(.02)
        for thread in threads:thread.join(timeout=5)
        if any(t.is_alive() for t in threads):raise RuntimeError('reader_pipe_drain_timeout')
        writer.write(output/'ffmpeg.log',bytes(tail),'xb')
        if errors:raise errors[0]
        if stop.is_set():return {'status':'STOPPED','source_reads':1,'frames':len(frames),'reservation_token':token}
        if process.returncode:
            after=source.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('source_changed_during_reader')
            stderr_text=bytes(tail).decode('utf-8',errors='replace')
            classification=decode_classification(process.returncode,stderr_text,decode_mode)
            if classification:
                raise DecodeFailure(dict(classification=classification,decode_mode=decode_mode,
                    command=args,exit_code=process.returncode,stderr=stderr_text,frames=len(frames),
                    reservation_token=token,reader_directory=str(output),at=time.time(),
                    codec_metadata=info.get('streams'),environment={k:os.environ.get(k) for k in ('PATH','FFREPORT')},
                    timeout_seconds=None,stop_interruptible=True))
            raise RuntimeError('ffmpeg_failed:'+str(process.returncode))
        if not frames:raise ValueError('frozen_sampling_produced_no_frame')
        after=source.stat()
        if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise ValueError('source_changed_during_reader')
        return {'status':'EXTRACTION_DONE','source_reads':1,'frames':len(frames),
            'audio_path':str(output/audio_name) if info['has_audio'] else None,
            'audio_ready_at':audio_ready_at[0],'video_eos_at':time.time(),'variant':variant,
            'audio_command':audio_args,
            'spool_bytes':writer.bytes,'reservation_bytes':ceiling,'reservation_token':token,
            'reader_directory':str(output),'short_time_ms':round(short*1000) if short is not None else None,
            'elapsed_seconds':time.monotonic()-start,'command':args,'decode_mode':decode_mode}
    finally:
        if write_fd is not None:os.close(write_fd)
        if process is None:os.close(read_fd)
        if process is not None and process.poll() is None:
            process.kill();process.wait(timeout=5)
        if audio_process is not None and audio_process.poll() is None:
            audio_process.terminate()
            try:audio_process.wait(timeout=5)
            except subprocess.TimeoutExpired:audio_process.kill();audio_process.wait(timeout=5)
        # Keep partial media/reservation for explicit recovery. Never delete.
