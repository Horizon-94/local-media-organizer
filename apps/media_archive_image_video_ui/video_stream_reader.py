"""Single sequential FFmpeg input, atomic frame publication and audio spool.

Isolated video experiment only: no source-count, people or closure gate.
The caller supplies a byte-spool reservation (including this source's worst
case output) BEFORE start; it must release reservations after durable consumers
retire their inputs. This intentionally does not assume JPEG compression ratio.
"""
from __future__ import annotations
from .stop_provenance import child_run
import json
from pathlib import Path
import subprocess
import time


def probe(source, ffprobe='ffprobe', *, stop=None):
    completed = child_run(stop,[ffprobe, '-v', 'error', '-show_entries',
        'format=duration:stream=codec_type,codec_name,profile,level,pix_fmt,bits_per_raw_sample,width,height,color_range,color_space,color_transfer,color_primaries,chroma_location,r_frame_rate,avg_frame_rate,nb_frames,duration', '-of', 'json', str(source)],
        capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(completed.stdout)
    result['duration_seconds'] = float(result['format']['duration'])
    result['has_audio'] = any(s['codec_type'] == 'audio' for s in result['streams'])
    return result


def command(source, output, *, has_audio, ffmpeg='ffmpeg'):
    output = Path(output)
    # Frozen Stage03 contract: 2s offset, 3s cadence, long edge 1280, JPEG q3.
    vf = "fps=1/3.0:start_time=2.0,scale='if(gt(iw,ih),1280,-2)':'if(gt(ih,iw),1280,-2)'"
    args = [ffmpeg, '-hide_banner', '-nostdin', '-n', '-i', str(source),
        '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', vf, '-q:v', '3',
        '-atomic_writing', '1', str(output/'frame_%06d.jpg')]
    if has_audio:
        args += ['-map', '0:a:0', '-vn', '-sn', '-dn', '-ac', '1', '-ar',
                 '16000', '-c:a', 'flac', '-compression_level', '5', str(output/'audio.flac')]
    return args


def reserved_bytes(duration_seconds):
    # JPEG can expand for adversarial data; conservative max raster+headers.
    # FLAC upper bound approximated conservatively as 2x uncompressed PCM.
    count = max(1, int(max(0, duration_seconds-2)//3)+2)
    return count*(1280*1280*3+65536)+int(duration_seconds*16000*2*2)+1048576


def run(source, output, *, on_frame, stop, reservation, ffmpeg='ffmpeg', ffprobe='ffprobe'):
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output == source.parent or source.parent in output.parents:
        raise ValueError('derived_output_must_not_be_in_source_directory')
    if output.exists():
        raise ValueError('new_attempt_directory_required')
    info = probe(source, ffprobe)
    if info['duration_seconds'] < 2:
        # Do not silently invent a new short-video sampling fallback.
        raise ValueError('short_video_sampling_adapter_required')
    size = reserved_bytes(info['duration_seconds'])
    token = reservation.acquire(size, stop)
    if token is None:
        return {'status': 'STOPPED_BEFORE_READ', 'source_reads': 0}
    before = source.stat()
    output.mkdir(parents=True)
    args = command(source, output, has_audio=info['has_audio'], ffmpeg=ffmpeg)
    published = set()
    started = time.monotonic()
    process = None
    try:
        with (output/'ffmpeg.log').open('wb') as log:
            process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True)
            (output/'ffmpeg.pid').write_text(str(process.pid))
            while True:
                for frame in sorted(output.glob('frame_*.jpg')):
                    if frame.name not in published:
                        # image2 atomic_writing exposes only completed JPEGs.
                        on_frame(frame, 2000+len(published)*3000)
                        published.add(frame.name)
                if stop.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    return {'status': 'STOPPED', 'source_reads': 1, 'frames': len(published)}
                if process.poll() is not None:
                    break
                stop.wait(.05)
        # Drain files which completed between last enumeration and exit.
        for frame in sorted(output.glob('frame_*.jpg')):
            if frame.name not in published:
                on_frame(frame, 2000+len(published)*3000)
                published.add(frame.name)
        if process.returncode:
            raise RuntimeError('ffmpeg_failed:'+str(process.returncode))
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise RuntimeError('source_changed_during_read')
        actual_bytes = sum(p.stat().st_size for p in output.iterdir() if p.is_file())
        if actual_bytes > size:
            raise RuntimeError('spool_reservation_exceeded')
        return dict(status='EXTRACTION_DONE', source_reads=1, frames=len(published),
            audio_path=str(output/'audio.flac') if info['has_audio'] else None,
            audio_status='READY' if info['has_audio'] else 'NO_AUDIO',
            elapsed_seconds=time.monotonic()-started, command=args,
            spool_bytes=actual_bytes, reservation_bytes=size, reservation_token=token)
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        # Retain partial artifacts and reservation for recovery, never delete.
        raise
