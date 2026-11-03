"""VIDEO-only index callbacks with strict scoped current-evidence proof.

An independent frame event has no reason to traverse every candidate's
project-wide dependency tree while holding the SQLite writer.
"""
import json
from .v2_incremental_index import IncrementalFusionIndex
from .v2_index_lineage import current_input_jobs
from .v2_producer import fingerprint
from .v2_runtime import ContractError


class VideoFusionIndex(IncrementalFusionIndex):
    def document_snapshot(self,c,did):
        # Exact unchanged-index proof for ONE document, never all source docs.
        import collections
        from .v2_index import SQLiteExactVectors
        if type(self.vectors) is not SQLiteExactVectors:return None
        row=c.execute('SELECT * FROM v2_documents WHERE document_id=?',(did,)).fetchone()
        if not row:return None
        doc=dict(row);raw,emb=current_input_jobs(c,[doc['raw_result_ref']],[doc['embedding_ref']] if doc['embedding_ref'] else [])
        fts=collections.defaultdict(list);coverage=collections.defaultdict(dict);vectors=collections.defaultdict(list)
        fts[did]=[tuple(r) for r in c.execute('SELECT document_id,source_id,text FROM v2_text_fts WHERE document_id=?',(did,))]
        coverage[did]={r['index_kind']:dict(r) for r in c.execute('SELECT * FROM v2_index_coverage WHERE entity_id=?',(did,))}
        vectors[did]=[dict(r) for r in c.execute('SELECT * FROM v2_vectors WHERE entity_id=?',(did,))]
        link=fingerprint((doc['source_id'],doc['source_revision'],did,doc['document_type']))
        fusion={r['link_id']:tuple(r) for r in c.execute('SELECT * FROM v2_fusion WHERE link_id=?',(link,))}
        return {did:doc},raw,emb,fts,coverage,vectors,fusion

    def rebuild(self,source,revision):
        from .video_detached_coordination import pipeline_operation
        def calculate(pipeline):
            index=pipeline.index
            with pipeline.store.connect() as c:
                for row in c.execute('SELECT document_id FROM v2_documents WHERE source_id=? AND source_revision=?',(source,revision)).fetchall():
                    index.index_document(c,row[0])
                events=[json.loads(row[0]) for row in c.execute("SELECT payload FROM v2_outbox WHERE aggregate_id=? AND event_type IN ('EVIDENCE_READY','PERSON_LINK_UPDATED') ORDER BY committed_at,event_id",(source+':'+revision,))]
                for event in events:index.callback(c,event)
        return pipeline_operation(self.store.adapter,source,revision,'index_rebuild',calculate)

    def fusion(self,c,source,revision,entity,artifact,start,end,kind,result_ref,payload):
        # Preserve coverage/business digest; only the persisted envelope uses
        # the existing canonical model ref instead of copying its body again.
        persisted=payload
        if isinstance(payload,dict) and 'result' in payload:
            raw=c.execute('SELECT result_json FROM v2_jobs WHERE execution_key=?',(result_ref,)).fetchone()
            # Read physical reference, bypass business hydration intentionally.
            if raw is not None and hasattr(raw,'values'):
                physical=json.loads(raw.values[0])
                if isinstance(physical,dict) and set(physical)=={'$canonical_result'}:
                    persisted={**payload,'result':physical}
        link=fingerprint((source,revision,entity,kind))
        c.execute('INSERT OR REPLACE INTO v2_fusion VALUES(?,?,?,?,?,?,?,?,?)',
            (link,source,revision,artifact,start,end,kind,result_ref,json.dumps(persisted,sort_keys=True)))
        self.covered(c,entity,'fusion',fingerprint(payload),'sqlite-timeline-v1')

    def callback(self,c,event):
        p=event['payload']
        source,revision=event['source_id'],event['source_revision']
        if event['event_type'] in ('DOCUMENT_READY','DOCUMENT_EMBEDDED'):
            # Document mutations have their own durable events. An unrelated
            # frame event cannot change this document's text or vector.
            if c.execute('SELECT 1 FROM v2_documents WHERE document_id=?',(p['document_id'],)).fetchone():
                previous=getattr(self,'_delivery_snapshot',None)
                self._delivery_snapshot=self.document_snapshot(c,p['document_id'])
                try:self.index_document(c,p['document_id'])
                finally:self._delivery_snapshot=previous
            return
        if event['event_type']!='EVIDENCE_READY' or p['stage'] not in ('yoloe','openclip','dhash','quality','geometry','insightface'):
            if event['event_type']=='SOURCE_READY':
                return self.fusion(c,event['source_id'],event['source_revision'],'source:'+event['source_id']+':'+event['source_revision'],None,None,None,'source_metadata',event['source_id'],p)
            if event['event_type']=='PERSON_LINK_UPDATED':
                # Person evidence cannot alter text/vector payloads. Preserve
                # the same current-membership proof without a document sweep.
                if not c.execute('SELECT 1 FROM v2_observation_heads WHERE observation_id=?',(p['observation_id'],)).fetchone():return
                from .v2_people import PersonIdentity
                people=PersonIdentity.__new__(PersonIdentity);people.store=self.store
                membership=c.execute('SELECT tracklet_id FROM v2_tracklet_membership WHERE observation_id=? ORDER BY version DESC LIMIT 1',(p['observation_id'],)).fetchone()
                current=c.execute('SELECT source_person_id FROM v2_tracklet_people WHERE tracklet_id=? ORDER BY version DESC LIMIT 1',(membership[0],)).fetchone() if membership else None
                if not current or current[0]!=p['source_person_id'] or people.person_global(c,current[0])!=p['global_person_id']:return
                return self.fusion(c,source,revision,p['observation_id'],p.get('artifact_id'),p.get('time_ms'),p.get('time_ms'),'person',p['source_person_id'],p)
            return
        source,revision=event['source_id'],event['source_revision']
        key=p['execution_key']
        current,_=current_input_jobs(c,[key],[])
        header=current.get(key)
        if not header or header['status']!='DONE' or header['result_sha']!=p['payload_sha']:return
        job=c.execute('SELECT * FROM v2_jobs WHERE execution_key=?',(key,)).fetchone()
        if job['source_id']!=source or job['source_revision']!=revision:raise ContractError('video_index_source_mismatch')
        artifact=c.execute('SELECT * FROM v2_artifacts WHERE artifact_id=?',(p['item_id'],)).fetchone()
        if not artifact:raise ContractError('fusion_artifact_missing')
        c.execute("DELETE FROM v2_fusion WHERE source_id=? AND source_revision=? AND EXISTS (SELECT 1 FROM v2_jobs j WHERE j.execution_key=v2_fusion.result_ref AND j.status='SUPERSEDED')",(source,revision))
        self.fusion(c,source,revision,key,artifact['artifact_id'],artifact['time_ms'],artifact['time_ms'],p['stage'],key,p)
        if p['stage']=='openclip':
            namespace='visual:'+fingerprint(json.loads(job['provenance']))
            self.vectors.upsert(c,artifact['artifact_id'],namespace,p['result']['vector'],p['payload_sha'])
            self.covered(c,artifact['artifact_id'],'visual-vector',p['payload_sha'],self.vectors.backend)
