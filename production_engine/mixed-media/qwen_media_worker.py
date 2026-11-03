"""One resident Qwen, per-source frozen correction policy, unchanged inference.

Transport extension only: real create_handler owns all model and validation
semantics. Mutable config is private to this serial single-worker process.
"""
import argparse,contextlib,json,sys
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))

def policy_for(kind,policies):
    if kind not in ('image','video') or set(policies)!={'image','video'}:raise ValueError('MIXED_QWEN_KIND_REQUIRED')
    from apps.media_archive_image_video_ui.v2_qwen_schema_retry import PERSISTENT_GROUP_POLICY,UNCERTAINTY_POLICY
    if policies['image']!=PERSISTENT_GROUP_POLICY:raise ValueError('IMAGE_QWEN_POLICY_NOT_FROZEN')
    # VIDEO uses its builder's existing frozen policy, not the IMAGE override.
    if not isinstance(policies['video'],str) or not policies['video']:raise ValueError('VIDEO_QWEN_POLICY_REQUIRED')
    return policies[kind]

def main():
    p=argparse.ArgumentParser();p.add_argument('--config-json',required=True);args=p.parse_args()
    cfg=json.loads(args.config_json);policies=cfg.pop('_mixed_policies');cfg.pop('_worker_ready_ack',None)
    from apps.media_archive_image_video_ui.v2_model_workers import create_handler
    with contextlib.redirect_stdout(sys.stderr):handler=create_handler('qwen',cfg)
    print(json.dumps({'ok':True,'worker_state':'READY'}),flush=True)
    for line in sys.stdin:
        try:
            payload=json.loads(line)
            if payload.get('request_kind')=='v2_worker_shutdown':
                print(json.dumps({'ok':True,'shutdown':'ack'}),flush=True);return
            kind=payload.pop('_mixed_media_kind',None)
            if not isinstance(payload.get('image_path'),str) or any(payload.get(k) for k in ('images','image_paths','context_images')):raise ValueError('QWEN_REQUIRES_ONE_IMAGE')
            cfg['schema_retry_policy']=policy_for(kind,policies)
            with contextlib.redirect_stdout(sys.stderr):result=handler(payload)
            response={'ok':True,'result':result}
        except Exception as e:response={'ok':False,'error':type(e).__name__+':'+str(e)}
        print(json.dumps(response,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
