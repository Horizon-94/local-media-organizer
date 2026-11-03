"""Read-only frontend projection of task-owned ingestion telemetry."""
import json,time
from pathlib import Path

def project(task,state,scheduler_active):
    if not scheduler_active:return None
    try:value=json.loads((Path(task['workspace']).parent/'ingestion_state.json').read_text())
    except (OSError,ValueError):return None
    if value.get('pid')!=state.get('worker_pid') or value.get('session_started',0)<state.get('started_at_epoch',0):return None
    phase=value.get('phase');labels={'DISCOVERING_SOURCES':'正在发现素材','INVENTORYING':'正在读取素材信息','VERIFYING_CONTENT':'正在完整读取并计算校验值','REUSING_CONTENT_IDENTITY':'文件未变化，沿用上次校验结果（不重读内容）','ENQUEUING':'正在提交已准备素材','WAITING_SOURCE_CLOSURE':'正在处理当前视频，等待闭环并回收中间音频','PROCESSING':'素材准备已完成','FINALIZING':'正在完成素材库闭环','ERROR':'素材准备遇到错误'}
    if phase not in labels:return None
    result={**value,'message':labels[phase],'engine_phase':phase,'filename':Path(value['current_source']).name if value.get('current_source') else None,'source_path':value.get('current_source'),'read_position':value.get('current_file_bytes_read'),'file_size':value.get('current_file_size'),'telemetry_source':'task-owned durable ingestion_state.json','phase_is_preparing':phase in ('DISCOVERING_SOURCES','INVENTORYING','VERIFYING_CONTENT','REUSING_CONTENT_IDENTITY','ENQUEUING')}
    if phase!='VERIFYING_CONTENT':result['read_position']=None;result['file_size']=None
    age=max(0,time.time()-value.get('last_progress_time',time.time()));result['progress_age_seconds']=age
    result['stall_warning']=phase in ('DISCOVERING_SOURCES','INVENTORYING','VERIFYING_CONTENT','REUSING_CONTENT_IDENTITY','ENQUEUING') and age>120
    if result['stall_warning']:result['warning']='进度超过 120 秒未更新；正在等待 I/O 或下游返回，尚不能据此断定磁盘故障。'
    return result
