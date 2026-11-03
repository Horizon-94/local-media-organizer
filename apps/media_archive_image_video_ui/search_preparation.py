"""Bounded App-session preparation of registered read-only libraries.

No vectors, media reads, task submissions or authoritative writes. A retained
proof is usable only inside the same connection's fresh SQLite snapshot.
"""
import json,copy,hashlib,os
from pathlib import Path
from collections import OrderedDict
from . import build151_library_lifecycle as libraries
from .build151_integrity_session import read_report,audit
from .search_session_store import SessionReadOnlyStore
from . import query_trace as trace

def file_identity(path,optional=False):
    p=Path(path).resolve()
    try:
        s=p.stat();fd=os.open(p,os.O_RDONLY);os.close(fd)
        return (str(p),s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_gid)
    except FileNotFoundError:
        if optional:return (str(p),None)
        raise

class PreparedLibraries:
    MAX_CONNECTIONS=32
    MAX_TEMP_BYTES=128*1024*1024
    def __init__(self,bridge):
        self.bridge=bridge;self.entries=OrderedDict();self.states={};self.registry_token=None;self.registry_rows=[]
    def registry_row(self,identity):
        token=file_identity(libraries.registry())
        if token!=self.registry_token:
            self.registry_rows=libraries.rows();self.registry_token=token
        found=[r for r in self.registry_rows if identity in (r.get('library_id'),r.get('task_id'),r.get('task_path'))]
        if len(found)!=1:raise ValueError('library_registry_identity_missing_or_ambiguous')
        return found[0]
    def context(self,entry):
        t=entry['task'];row=self.registry_row(entry['record']['library_id'])
        paths=[(entry['task_path'],False),(Path(t['database']).parent/'model-config.json',False),
               (Path(t['workspace'])/'contracts.json',True),(t['state_path'],True),
               (Path(t['workspace'])/'run-config.json',True)]
        return (json.dumps(row,sort_keys=True),tuple(file_identity(p,optional) for p,optional in paths),entry['runtime_identity'])
    def _new(self,value,base,manager):
        record,path,task=libraries.resolve(value)
        record={**record,'task_path':str(path),'database':task['database'],'task_name':task['name']}
        namespace=hashlib.sha256(str(Path(task['database']).resolve()).encode()).hexdigest()[:24]
        config={**base,'library_id':record['library_id'],'task_id':record['library_id'],'task_path':str(path),'library_task_path':str(path),
                'database':task['database'],'output_root':task['workspace'],'task_state_path':task['state_path'],
                'search_cache_root':str(self.bridge.search_runtime_cache_root(base)/'libraries'/namespace)}
        if record['library_id']==base.get('library_id'):
            config['search_cache_root']=str(self.bridge.search_runtime_cache_root(base))
        local=self.bridge.SearchJobManager(db_path=Path(task['database']),output_root=manager.output_root/'prepared'/namespace,
              search_script=manager.search_script,search_config=manager.search_config,embedding_python=manager.embedding_python,openclip_python=manager.openclip_python)
        store=SessionReadOnlyStore(local.db_path);store.reuse_preparation=True;local.session_store=store
        entry={'record':record,'task':task,'task_path':str(path),'config':config,'manager':local,'store':store,
               'runtime_identity':json.dumps({k:str(getattr(manager,k)) for k in ['embedding_python','openclip_python','search_script','search_config']},sort_keys=True)}
        store.context_token=lambda:self.context(entry)
        entry['context']=self.context(entry)
        return entry
    def get(self,value,base,manager):
        value=value or base.get('library_task_path') or base.get('task_path')
        if not value:raise ValueError('no_selected_library')
        key=str(value);entry=self.entries.get(key)
        if entry is not None:
            try:
                runtime=json.dumps({k:str(getattr(manager,k)) for k in ['embedding_python','openclip_python','search_script','search_config']},sort_keys=True)
                changed=self.context(entry)!=entry['context'] or runtime!=entry['runtime_identity']
            except Exception:
                self.invalidate(key);raise
            if changed:self.invalidate(key);entry=None
        if entry is None:
            entry=self._new(value,base,manager);self.entries[key]=entry
        self.entries.move_to_end(key)
        try:
            proof=read_report(entry['manager'].db_path)
            if proof.get('integrity_check')=='pending' and entry['manager'].db_path.stat().st_size<=64*1024*1024:
                with trace.span('library_integrity_prepare'):proof=audit(entry['manager'].db_path)
            if proof.get('integrity_check')!='ok' or proof.get('foreign_key_error_count')!=0:
                raise RuntimeError('library_integrity_not_ready:缺少可信完整性凭证，请打开此库完成校验；不自动重建')
            with trace.span('library_prepare_readiness'):
                readiness=entry['manager'].readiness()
            if not readiness.get('ready'):raise RuntimeError('library_has_no_searchable_published_sources：此库尚无已发布的可搜索来源')
            entry['readiness']=readiness;entry['integrity']=proof
            self.states[key]={'task_path':entry['task_path'],'name':entry['record']['task_name'],'state':'ready','reason':'','retained':True}
            return key,entry
        except BaseException as error:
            self.states[key]={'task_path':key,'name':entry['record']['task_name'],'state':'unavailable','reason':str(error),'retained':False}
            self.invalidate(key);raise
    def finish(self,key,success=True):
        if key in self.entries:self.entries[key]['store'].finish(success)
        self.trim(keep=key)
    def invalidate(self,key):
        entry=self.entries.pop(key,None)
        if entry is not None:entry['store'].close()
    def trim(self,keep=None):
        def size():
            total=0
            for e in self.entries.values():
                c=e['store'].connection
                if c is not None:total+=int(c.execute('PRAGMA temp.page_count').fetchone()[0])*int(c.execute('PRAGMA temp.page_size').fetchone()[0])
            return total
        while len(self.entries)>self.MAX_CONNECTIONS or size()>self.MAX_TEMP_BYTES:
            key=next(iter(self.entries))
            if key==keep and len(self.entries)>1:self.entries.move_to_end(key);continue
            self.invalidate(key)
            if key in self.states:self.states[key].update(state='pending',reason='准备缓存达到上限，查询时有界恢复',retained=False)
            if not self.entries:break
    def status(self):
        for key,e in list(self.entries.items()):
            try:current=e['store'].preparation_current()
            except Exception as error:
                self.states[key].update(state='unavailable',reason=str(error),retained=False);self.invalidate(key);continue
            if not current:self.states[key].update(state='pending',reason='数据库或配置已变化，需要重新准备')
        return copy.deepcopy(list(self.states.values()))
    def close(self):
        for key in list(self.entries):self.invalidate(key)
