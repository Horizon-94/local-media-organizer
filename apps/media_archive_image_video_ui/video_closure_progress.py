"""Read-only durable local closure gates, separate from model queue state."""
def pending(c):
    result={}
    sources=list(c.execute('SELECT source_id,revision FROM video_stream_sources_v1'))
    for source,revision in sources:
        states={r[0] for r in c.execute('SELECT state FROM video_stream_source_states_v1 WHERE source_id=? AND revision=?',(source,revision))}
        if 'SOURCE_COMPLETE' in states:continue
        gates={}
        for state in ('VIDEO_BRANCH_EOS','EXTRACTION_DONE','SELECTION_DONE','LOCAL_PEOPLE_DONE','OCR_COMPLETE'):
            if state not in states:gates[state]=1
        docs=list(c.execute("SELECT document_id,status,embedding_ref FROM v2_documents WHERE source_id=? AND source_revision=? AND status<>'SUPERSEDED'",(source,revision)))
        for did,status,ref in docs:
            if status not in ('EMBEDDED','SKIP_NO_TEXT'):gates['documents']=gates.get('documents',0)+1
            if status=='SKIP_NO_TEXT':continue
            for kind in ('fts','fusion','vector'):
                if not c.execute("SELECT 1 FROM v2_index_coverage WHERE entity_id=? AND index_kind=? AND status='READY'",(did,kind)).fetchone():gates[kind]=gates.get(kind,0)+1
        if not c.execute('SELECT 1 FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(source,revision)).fetchone():gates['final_projection']=1
        gates['source_complete']=1
        result[source]=gates
    return result
