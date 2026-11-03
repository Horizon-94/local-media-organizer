"""Product adapter: do not repeatedly retire already-empty historical documents."""
import json
import time
import collections
import math
from .v2_index import FusionIndex, SQLiteExactVectors, fts_tokens
from .v2_producer import fingerprint
from .v2_runtime import ContractError


class IncrementalFusionIndex(FusionIndex):
    def metric(self,name,n=1):
        perf=getattr(self,'performance',None)
        if perf is not None:perf.count(name,n)

    def _open_source_events(self,limit):
        """Execution-order hint only; original consume remains authority.

        Three source-completion quanta followed by one global FIFO quantum
        prevent index interleaving from keeping every producer slot occupied.
        Standalone and explicitly scoped repair adapters retain their path.
        """
        owner=getattr(self.store,'scheduler_owner',None)
        if not owner or getattr(self,'repair_source_scope',None) is not None:return None
        turn=getattr(self,'_index_affinity_turn',0)+1
        self._index_affinity_turn=turn
        if turn%4==0:return None
        with self.store.connect() as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='v2_product_sources'").fetchone():return None
            if not getattr(self,'_index_affinity_schema',False):
                c.execute('''CREATE INDEX IF NOT EXISTS v2_outbox_owner_aggregate_order
                    ON v2_outbox(scheduler_run_id,execution_epoch,aggregate_id,committed_at,event_id)''')
            rid,epoch=owner['scheduler_run_id'],owner['execution_epoch']
            source=c.execute('''SELECT p.source_id,p.source_revision FROM v2_product_sources p
                JOIN v2_active_sources a ON a.source_id=p.source_id AND a.source_revision=p.source_revision
                WHERE p.scheduler_run_id=? AND p.state='INGESTED' AND EXISTS(
                    SELECT 1 FROM v2_outbox o INDEXED BY v2_outbox_owner_aggregate_order
                    WHERE o.scheduler_run_id=? AND o.execution_epoch=?
                    AND o.aggregate_id=p.source_id||':'||p.source_revision
                    AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='fusion-index' AND i.event_id=o.event_id))
                ORDER BY p.source_id LIMIT 1''',(rid,rid,epoch)).fetchone()
            if not source:return None
            events=[json.loads(r[0]) for r in c.execute('''SELECT o.payload FROM v2_outbox o
                INDEXED BY v2_outbox_owner_aggregate_order
                WHERE o.scheduler_run_id=? AND o.execution_epoch=? AND o.aggregate_id=?
                AND NOT EXISTS(SELECT 1 FROM v2_inbox i WHERE i.consumer='fusion-index' AND i.event_id=o.event_id)
                ORDER BY o.committed_at,o.event_id LIMIT ?''',(rid,epoch,source[0]+':'+source[1],limit))]
        if events:
            self._index_source_hint=(source[0],source[1])
            self.metric('INDEX_SOURCE_AFFINITY_QUANTA')
        return events or None

    def advance(self, limit=100):
        # One delivery may itself be costly. Bound the cooperative quantum by
        # wall clock, not merely eight callbacks, without splitting a commit.
        if limit<=0:return 0
        if limit>8:return super().advance(limit=limit)
        deadline=time.monotonic()+0.1
        count=0
        class BudgetYield(Exception):pass
        def bounded_callback(c,event):
            nonlocal count
            # Discovery is paid once. Yield BEFORE the next callback: consume
            # rolls back that empty transaction and leaves its inbox absent.
            # Always finish the first event even if discovery used the budget.
            if count and time.monotonic()>=deadline:raise BudgetYield()
            self.callback(c,event)
            count+=1
        try:
            events=self._open_source_events(limit)
            if events is not None:
                for event in events:
                    self.store.consume(event,'fusion-index',bounded_callback)
                return count
            return self.store.deliver('fusion-index',bounded_callback,limit=limit)
        except BudgetYield:
            return count

    def _materialization_snapshot(self,c,source,revision):
        # Always read the proof through the caller's current transaction.
        # This is shared by event delivery and the source-completion barrier.
        snapshot=None
        if type(getattr(self,'vectors',None)) is SQLiteExactVectors:
            docs={r['document_id']:dict(r) for r in c.execute('SELECT * FROM v2_documents WHERE source_id=? AND source_revision=?',(source,revision))}
            raw_refs=list({d['raw_result_ref'] for d in docs.values()})
            embed_refs=list({d['embedding_ref'] for d in docs.values() if d['embedding_ref']})
            from .v2_index_lineage import current_input_jobs
            raw_jobs,embeddings=current_input_jobs(c,raw_refs,embed_refs)
            fts=collections.defaultdict(list)
            for r in c.execute('SELECT document_id,source_id,text FROM v2_text_fts WHERE source_id=?',(source,)):fts[r[0]].append(tuple(r))
            coverage=collections.defaultdict(dict)
            for r in c.execute('SELECT x.* FROM v2_index_coverage x JOIN v2_documents d ON d.document_id=x.entity_id WHERE d.source_id=? AND d.source_revision=?',(source,revision)):coverage[r['entity_id']][r['index_kind']]=dict(r)
            vectors=collections.defaultdict(list)
            for r in c.execute('SELECT x.* FROM v2_vectors x JOIN v2_documents d ON d.document_id=x.entity_id WHERE d.source_id=? AND d.source_revision=?',(source,revision)):vectors[r['entity_id']].append(dict(r))
            fusion={r['link_id']:tuple(r) for r in c.execute('SELECT * FROM v2_fusion WHERE result_ref IN (SELECT value FROM json_each(?))',(json.dumps(raw_refs),))}
            snapshot=(docs,raw_jobs,embeddings,fts,coverage,vectors,fusion)
        return snapshot

    def close_source(self,*args,**kwargs):
        previous_snapshot=getattr(self,'_delivery_snapshot',None)
        previous_join=getattr(self,'_join_snapshot_enabled',False)
        self._delivery_snapshot=None
        self._join_snapshot_enabled=True
        try:
            # The original barrier still performs every evidence/revision,
            # audio and coverage check. Only redundant index writes are skipped.
            return super().close_source(*args,**kwargs)
        finally:
            self._delivery_snapshot=previous_snapshot
            self._join_snapshot_enabled=previous_join

    def callback(self, c, event):
        self.metric('INDEX_CALLBACK_COUNT')
        source, revision = event['source_id'], event['source_revision']
        previous_snapshot=getattr(self,'_delivery_snapshot',None)
        snapshot=self._materialization_snapshot(c,source,revision)
        # Proof is rebuilt inside the delivery transaction, never cached across
        # revisions. Even a legacy orphan FTS row forces the original cleanup.
        live_fts = {r[0] for r in c.execute(
            'SELECT document_id FROM v2_text_fts WHERE source_id=?', (source,))}
        retired = {r[0] for r in c.execute('''
            SELECT d.document_id FROM v2_documents d
            WHERE d.source_id=? AND d.source_revision=? AND d.status='SUPERSEDED'
              AND NOT EXISTS (SELECT 1 FROM v2_index_coverage x WHERE x.entity_id=d.document_id)
              AND NOT EXISTS (SELECT 1 FROM v2_vectors x WHERE x.entity_id=d.document_id)
              AND NOT EXISTS (SELECT 1 FROM v2_fusion x WHERE x.result_ref=d.raw_result_ref)
            ''', (source, revision))} - live_fts
        previous = getattr(self, '_retired_in_delivery', frozenset())
        self._retired_in_delivery = retired
        self._delivery_snapshot=snapshot
        try:
            return super().callback(c, event)
        finally:
            self._retired_in_delivery = previous
            self._delivery_snapshot=previous_snapshot

    def _already_materialized(self,did):
        snapshot=getattr(self,'_delivery_snapshot',None)
        if snapshot is None:return False
        docs,jobs,embeddings,fts,coverage,vectors,fusion=snapshot
        doc=docs.get(did)
        if not doc:return False
        job=jobs.get(doc['raw_result_ref'])
        if not job or job['status']!='DONE':return False
        def covered(kind,digest,backend):
            row=coverage[did].get(kind)
            return row and row['status']=='READY' and row['input_digest']==digest and row['backend']==backend
        digest=fingerprint((doc['revision'],doc['text'],job['result_sha']))
        if not covered('fts',digest,'sqlite-fts5-cjk-bigram-v1'):return False
        if fts[did]!=[(did,doc['source_id'],fts_tokens(doc['text']))]:return False
        if not covered('fusion',fingerprint(doc),'sqlite-timeline-v1'):return False
        link=fingerprint((doc['source_id'],doc['source_revision'],did,doc['document_type']))
        expected=(link,doc['source_id'],doc['source_revision'],doc['artifact_id'],doc['start_ms'],doc['end_ms'],doc['document_type'],doc['raw_result_ref'],json.dumps(doc,sort_keys=True))
        if fusion.get(link)!=expected:return False
        if doc['embedding_ref']:
            embedding=embeddings.get(doc['embedding_ref'])
            if not embedding or embedding['status']!='DONE':
                return not vectors[did] and 'vector' not in coverage[did]
            if not covered('vector',embedding['result_sha'],self.vectors.backend):return False
            vector=json.loads(embedding['result_json'])['vector']
            if not vector or not all(math.isfinite(x) for x in vector):return False
            norm=math.sqrt(sum(x*x for x in vector))
            if not norm:return False
            namespace='text:'+fingerprint(json.loads(embedding['provenance']))
            expected_vector={'entity_id':did,'namespace':namespace,'dimension':len(vector),
                'vector_json':json.dumps([x/norm for x in vector]),'input_digest':embedding['result_sha']}
            if vectors[did]!=[expected_vector]:return False
        return True

    def index_document(self,c,did):
        started=time.perf_counter()
        try:return self._index_document(c,did)
        finally:
            if hasattr(c,'item_timings'):c.item_timings.append({'document_id':did,'compute_ms':(time.perf_counter()-started)*1000})

    def _index_document(self, c, did):
        if did in getattr(self, '_retired_in_delivery', ()):
            self.metric('RETIRED_INDEX_SKIP_COUNT')
            return False
        if getattr(self,'_join_snapshot_enabled',False) and getattr(self,'_delivery_snapshot',None) is None:
            row=c.execute('SELECT source_id,source_revision FROM v2_documents WHERE document_id=?',(did,)).fetchone()
            if row:
                self._delivery_snapshot=self._materialization_snapshot(c,row[0],row[1])
        checked=time.perf_counter();materialized=self._already_materialized(did)
        if hasattr(c,'phase_timings'):c.phase_timings['coverage_check_ms']=c.phase_timings.get('coverage_check_ms',0)+(time.perf_counter()-checked)*1000
        if materialized:
            self.metric('UNCHANGED_INDEX_SKIP_COUNT')
            return True
        doc=c.execute('SELECT * FROM v2_documents WHERE document_id=?',(did,)).fetchone()
        if not doc:raise ContractError('document_missing')
        # Indexing needs raw-result identity, NOT OCR pixel evidence/payload.
        # Preserve the frozen writes and checks with narrow live headers.
        job=c.execute('SELECT status,result_sha FROM v2_current_jobs WHERE execution_key=?',(doc['raw_result_ref'],)).fetchone()
        snapshot=getattr(self,'_delivery_snapshot',None)
        existing=snapshot and (snapshot[3][did] or snapshot[4][did] or snapshot[5][did])
        if not job or job['status']!='DONE':
            if existing:self.metric('INVALID_SOURCE_REPAIR_COUNT')
            c.execute('DELETE FROM v2_text_fts WHERE document_id=?',(did,));self.vectors.delete(c,did)
            c.execute('DELETE FROM v2_index_coverage WHERE entity_id=?',(did,))
            c.execute('DELETE FROM v2_fusion WHERE result_ref=?',(doc['raw_result_ref'],))
            return False
        self.metric('DAMAGED_INDEX_REPAIR_COUNT' if existing else 'NEW_DOCUMENT_INDEX_COUNT')
        digest=fingerprint((doc['revision'],doc['text'],job['result_sha']))
        c.execute('DELETE FROM v2_text_fts WHERE document_id=?',(did,))
        c.execute('INSERT INTO v2_text_fts VALUES(?,?,?)',(did,doc['source_id'],fts_tokens(doc['text'])))
        self.covered(c,did,'fts',digest,'sqlite-fts5-cjk-bigram-v1')
        self.fusion(c,doc['source_id'],doc['source_revision'],did,doc['artifact_id'],doc['start_ms'],doc['end_ms'],doc['document_type'],doc['raw_result_ref'],dict(doc))
        if doc['embedding_ref']:
            embedding=c.execute('SELECT status,provenance,result_json,result_sha FROM v2_current_jobs WHERE execution_key=?',(doc['embedding_ref'],)).fetchone()
            self.vectors.delete(c,did)
            c.execute("DELETE FROM v2_index_coverage WHERE entity_id=? AND index_kind='vector'",(did,))
            if embedding and embedding['status']=='DONE':
                namespace='text:'+fingerprint(json.loads(embedding['provenance']))
                self.vectors.upsert(c,did,namespace,json.loads(embedding['result_json'])['vector'],embedding['result_sha'])
                self.covered(c,did,'vector',embedding['result_sha'],self.vectors.backend)
        return True
