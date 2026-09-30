#!/usr/bin/env python3
"""Select three existing long audio-bearing inventory entries, no media scan.

Audio-bearing does not prove speech. Formal reports must subsequently prove
at least two speech/inference inputs before interpreting Whisper x2 scaling.
"""
import argparse
import json
import subprocess
import sys
import hashlib
import shutil
from pathlib import Path


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--inventory',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--validated-run',type=Path)
    ap.add_argument('--publish',type=Path)
    ap.add_argument('--select',action='append',help='Existing inventory relative path; repeat exactly three times')
    args=ap.parse_args()
    if args.validated_run:
        report=json.loads((args.validated_run/'stage/audio_enrichment_summary.json').read_text())
        speech=report.get('counts',{}).get('success',0)
        if report.get('status')!='SUCCESS' or speech<2: raise RuntimeError('audio_fixture_not_enough_verified_speech')
        if not args.publish: raise RuntimeError('publish_root_required')
        args.publish.mkdir(parents=True,exist_ok=True)
        template=args.publish/'pristine.sqlite'
        if template.exists(): raise RuntimeError('refuse_overwrite_published_audio_template')
        shutil.copy2(args.out/'pristine.sqlite',template)
        (args.publish/'manifest.json').write_text(json.dumps({'speech_inference_inputs':speech,'source_video_count':report['source_video_count'],
            'template_sha256':hashlib.sha256(template.read_bytes()).hexdigest(),'validation_run':str(args.validated_run)},indent=2)+'\n')
        print('AUDIO_FIXTURE_SPEECH_VALIDATION = PASS')
        return
    data=json.loads(args.inventory.read_text())
    candidates=[r for r in data['video_inventory'] if r.get('has_audio') and not r.get('probe_error')]
    candidates.sort(key=lambda r:r.get('duration_seconds') or 0,reverse=True)
    selected=candidates[:3] if not args.select else [next(r for r in candidates if r['relative_path']==name) for name in args.select]
    if len(selected)!=3 or sum(r['bytes'] for r in selected)>10*1024**3:
        raise RuntimeError('bounded_audio_fixture_selection_failed')
    args.out.mkdir(parents=True,exist_ok=True)
    inventory=args.out/'selected-inventory.json'
    inventory.write_text(json.dumps({'source':data['source'],'sample':{'videos':selected,'images':[]}},indent=2)+'\n')
    db=args.out/'pristine.sqlite'
    if db.exists(): raise RuntimeError('refuse_overwrite_audio_template')
    subprocess.run([sys.executable,str(Path(__file__).with_name('build_media_fixture.py')),
                    '--inventory',str(inventory),'--db',str(db)],check=True)
    print('SPEECH_ELIGIBILITY = PENDING_FORMAL_VAD_AND_WHISPER')
    print(f'AUDIO_TEMPLATE = {db}')


if __name__=='__main__': main()
