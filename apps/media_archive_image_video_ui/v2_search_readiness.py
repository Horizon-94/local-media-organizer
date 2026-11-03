"""Readiness for the existing V2 current-document keyword search route."""
from .v2_index import fts_tokens
from .v2_producer import fingerprint


def current_text_readiness(c):
    required={'v2_current_jobs','v2_searchable_documents','v2_text_fts','v2_index_coverage','v2_active_sources'}
    present={r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    checks={'v2_current_authority':required<=present,'v2_current_documents':False,'v2_fts_content_coverage':False}
    if not checks['v2_current_authority']:return checks
    view='release_searchable_documents' if 'release_searchable_documents' in present else 'v2_searchable_documents'
    rows=c.execute(f"""SELECT d.document_id,d.source_id,d.revision,d.text,j.result_sha,
        f.source_id,f.text,x.input_digest,x.backend,x.status
        FROM {view} d JOIN v2_active_sources a
          ON a.source_id=d.source_id AND a.source_revision=d.source_revision
        JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref AND j.status='DONE'
        LEFT JOIN v2_text_fts f ON f.document_id=d.document_id
        LEFT JOIN v2_index_coverage x ON x.entity_id=d.document_id AND x.index_kind='fts'
        WHERE d.status<>'SUPERSEDED'""").fetchall()
    checks['v2_current_documents']=bool(rows)
    seen=set();valid=bool(rows)
    for did,source,revision,text,digest,fts_source,fts_text,index_digest,backend,status in rows:
        valid=valid and did not in seen and fts_source==source and fts_text==fts_tokens(text)
        valid=valid and status=='READY' and backend=='sqlite-fts5-cjk-bigram-v1'
        valid=valid and index_digest==fingerprint((revision,text,digest))
        seen.add(did)
    checks['v2_fts_content_coverage']=bool(valid)
    return checks
