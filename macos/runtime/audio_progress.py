"""Read-only project-wide audio progress; no scheduling or model decisions."""
import json

def project(c):
 rows=list(c.execute("SELECT r.source_id,r.revision,b.status,b.segments_json FROM release_registry r LEFT JOIN v2_audio_branches b ON b.source_id=r.source_id AND b.source_revision=r.revision WHERE r.present=1 AND json_extract(r.row_json,'$.media_kind')='video'"))
 jobs={}
 for sid,rev,stage,status,n in c.execute("SELECT j.source_id,j.revision,j.stage,j.status,COUNT(*) FROM video_stream_jobs_v1 j JOIN release_registry r ON r.source_id=j.source_id AND r.revision=j.revision WHERE r.present=1 AND j.stage IN ('audio_extract','vad','whisper') AND j.status<>'SUPERSEDED' GROUP BY j.source_id,j.revision,j.stage,j.status"):
  jobs.setdefault((sid,rev),[]).append((stage,status,n))
 out=dict(total=len(rows),checked=0,complete=0,no_speech=0,no_audio=0,speech_sources=0,whisper_jobs=0)
 for sid,rev,status,segments in rows:
  current=jobs.get((sid,rev),[])
  out['whisper_jobs']+=sum(n for stage,_,n in current if stage=='whisper')
  if status not in ('NO_AUDIO','NO_SPEECH','SEGMENTS_READY'):continue
  out['checked']+=1
  settled=all(state=='DONE' for _,state,_ in current)
  if status=='NO_AUDIO':out['no_audio']+=1;out['complete']+=int(settled)
  elif status=='NO_SPEECH':out['no_speech']+=1;out['complete']+=int(settled)
  else:
   out['speech_sources']+=1
   try:count=len(json.loads(segments or '[]'))
   except (TypeError,ValueError):continue
   finished=sum(n for stage,state,n in current if stage=='whisper' and state=='DONE')
   pending=any(state!='DONE' for _,state,_ in current)
   if count>0 and finished>=count and not pending:out['complete']+=1
 out['unchecked']=max(0,out['total']-out['checked'])
 if out['whisper_jobs']==0 and out['no_speech']>0 and out['speech_sources']==0:
  out['whisper_note']='已检查的视频未检出有效人声片段，因此没有创建转写任务。'+('其余视频仍待检查。' if out['unchecked'] else '')
 elif out['whisper_jobs']==0:
  out['whisper_note']='目前尚未创建转写任务；不能据此认定所有视频的声音处理已完成。'
 else:out['whisper_note']='Whisper只处理后台实际生成的人声片段；空闲不等于整个项目已完成。'
 return out
