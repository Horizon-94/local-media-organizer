from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from urllib.parse import quote
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .search_result_contract import PAGE_SIZE, source_results, page


def _install_search_lineage_view(con):
    """Use the production correlated proof without writing the task database."""
    columns = {row[1] for row in con.execute('PRAGMA table_info(v2_jobs)')}
    tables = {row[0] for row in con.execute('SELECT name FROM sqlite_master')}
    if {'execution_key', 'status', 'source_id', 'source_revision'} <= columns and 'v2_job_dependencies' in tables:
        from .v2_current_jobs_cache import install_readonly_snapshot_view
        install_readonly_snapshot_view(con)


def _published_filename_rows(c, query):
    """Exact basename metadata channel; no original filesystem access."""
    name=query.strip()
    if not name or '/' in name or '\\' in name or not Path(name).suffix:return []
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'release_search_heads','release_registry','v2_active_sources','v2_source_metadata','release_search_segments'}<=tables:return []
    result=[]
    for source,revision,relative,generation in c.execute("""SELECT h.source_id,h.revision,r.relative_path,h.generation
        FROM release_search_heads h JOIN release_registry r ON r.source_id=h.source_id AND r.revision=h.revision
        JOIN v2_active_sources a ON a.source_id=h.source_id AND a.source_revision=h.revision
        WHERE h.visible=1 AND r.present=1 AND r.committed=1 ORDER BY h.source_id,h.revision"""):
        if str(relative or '').rsplit('/',1)[-1].casefold()!=name.casefold():continue
        segment=c.execute("""SELECT artifact_id,start_ms,end_ms FROM release_search_segments
            WHERE source_id=? AND revision=? AND generation=? ORDER BY start_ms,segment_id LIMIT 1""",(source,revision,generation)).fetchone()
        result.append(dict(document_id='filename:'+source+':'+revision,source_id=source,source_revision=revision,
            artifact_id=segment[0] if segment else None,start_ms=segment[1] if segment else None,
            end_ms=segment[2] if segment else None,document_type='source_filename',text=str(relative),rank=0))
    return result


def fast_filename_preview(database,query,request):
    """Published filename metadata needs no model ancestry scan.

    OCR/person predicates retain the full reader. This is provisional metadata,
    never an alternate final search or a replacement for document validation.
    """
    name=query.strip()
    if (not Path(name).suffix or '/' in name or '\\' in name or request.get('has_ocr')
            or request.get('has_person') or request.get('media_type')=='audio' or request.get('offset',0)):
        return []
    c=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
    c.row_factory=sqlite3.Row
    try:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        rows=_published_filename_rows(c,name);items=[]
        for row in rows:
            raw=c.execute('SELECT path,metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?',(row['source_id'],row['source_revision'])).fetchone()
            if not raw:continue
            meta=json.loads(raw['metadata']);kind=meta.get('media_type',meta.get('media_kind','image'))
            if request.get('media_type','all') not in ('all',kind):continue
            relative=str(meta.get('source_relative_path') or '')
            if not relative.startswith(str(request.get('path_prefix') or '')):continue
            mtime=meta.get('source_mtime')
            if mtime is None and meta.get('source_mtime_ns') is not None:mtime=int(meta['source_mtime_ns'])/1000000000
            if any(request.get(k) is not None and (mtime is None or (mtime<int(request[k]) if k.endswith('min') else mtime>int(request[k]))) for k in ('source_mtime_min','source_mtime_max')):continue
            artifact=c.execute('SELECT path FROM v2_artifacts WHERE artifact_id=?',(row['artifact_id'],)).fetchone() if row['artifact_id'] else None
            from .library_path_alias import resolve
            preview=resolve(database,artifact[0]) if artifact else ''
            items.append({'result_id':row['document_id'],'source_content_id':row['source_id'],
                'source_path':raw['path'],'source_relative_path':relative,'media_type':kind,
                'preview_path':preview,'visual_unit_id':row['artifact_id'],'text_preview':relative,
                'source_revision':row['source_revision'],'evidence_category':'filename','search_intent':request.get('search_intent','visual'),
                'time_position_ms':row['start_ms'],'result_start_ms':row['start_ms'],'result_end_ms':row['end_ms'],
                'score':1.0/61,'match_reasons':[{'type':'SOURCE_FILENAME','authority':'published_source_metadata','text':relative}]})
        result=source_results(items)
        for item in result:
            original=str(item.get('source_path') or '')
            item.update(source_online=False,source_availability='unchecked',can_open_original=bool(original),original_path=original)
        return result
    finally:c.close()


class _PublishedLexicalStore:
    """Bounded early text preview with the original exact ancestry predicate.

    No vector binding scan or materialized all-job proof is needed to display
    a published lexical hit. Final readiness/vector work remains unchanged.
    """
    def __init__(self, path):
        self.path=Path(path);self.connection=None;self.deadline=time.monotonic()+1.0
    @contextmanager
    def connect(self):
        if self.connection is None:
            c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.2)
            self.connection=c;c.row_factory=sqlite3.Row
            c.set_progress_handler(lambda:int(time.monotonic()>self.deadline),1000)
            c.execute('BEGIN')
            from .v2_current_jobs_cache import correlated_predicate
            c.execute('CREATE TEMP VIEW v2_current_jobs AS SELECT j.* FROM main.v2_jobs j WHERE '+correlated_predicate(c))
            c.execute('PRAGMA query_only=ON')
        yield self.connection
    def close(self):
        if self.connection is not None:self.connection.close();self.connection=None


def fast_text_preview(manager,query,request):
    # Only the explicit text mode needs this cold-start lexical shortcut.
    # Complex optional filters keep their original path; no filter is ignored.
    if request.get('search_intent')!='text' or request.get('offset',0) or request.get('has_person') or request.get('has_ocr'):
        return []
    import tempfile
    store=_PublishedLexicalStore(manager.db_path)
    channels=getattr(manager,'search_channels',None)
    try:
        with tempfile.TemporaryDirectory(prefix='build151-text-preview-') as temp:
            root=Path(temp)
            job=SearchJob(job_id='published_lexical_preview',query_sha256='',status='running',created_at=time.time(),output_dir=root,log_path=root/'preview.log')
            if not manager._run_v2_current_search(job,query,{**request,'_lexical_only':True,'_early_text_preview':True},_store=store):return []
            result=json.loads(job.response_json.read_text())['result_items']
            for item in result:
                original=str(item.get('source_path') or '')
                item.update(source_online=False,source_availability='unchecked',can_open_original=bool(original),original_path=original)
            return result
    finally:
        store.close()
        if channels is None:manager.__dict__.pop('search_channels',None)
        else:manager.search_channels=channels


class _RequestReadOnlyStore:
    """One exact read snapshot per search, shared with its lexical preview.

    No cross-request cache. SQL scopes retain their existing 30-second budget;
    model inference outside a SQL scope does not consume that SQL budget.
    """
    def __init__(self,path):
        self.path=Path(path);self.connection=None;self.deadlines=[]
    @contextmanager
    def connect(self):
        self.deadlines.append(time.monotonic()+30)
        try:
            if self.connection is None:
                con=sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro",uri=True)
                self.connection=con;con.row_factory=sqlite3.Row
                con.set_progress_handler(lambda:int(time.monotonic()>self.deadlines[-1]),10000)
                con.execute('BEGIN');_install_search_lineage_view(con)
                con.execute('PRAGMA query_only=ON')
            con=self.connection
            if not con.in_transaction:raise RuntimeError('search_snapshot_transaction_lost')
            con.set_progress_handler(lambda:int(time.monotonic()>self.deadlines[-1]),10000)
            yield con
        finally:
            self.deadlines.pop()
            if self.connection is not None and not self.deadlines:self.connection.set_progress_handler(None,0)
    def close(self):
        if self.connection is not None:
            self.connection.close();self.connection=None


def offline_search_environment(cache_root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    # Embedded Python may expose additional PYTHON* variables depending on the
    # macOS/Xcode runtime.  A child virtual environment must derive its prefix
    # from its own executable, never from the app's interpreter state.
    for inherited in tuple(environment):
        if inherited.startswith("PYTHON") or inherited in {"__PYVENV_LAUNCHER__", "VIRTUAL_ENV"}:
            environment.pop(inherited, None)
    environment.update({
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
        "HF_HOME": str(cache_root / "cache" / "huggingface"),
        "TORCH_HOME": str(cache_root / "cache" / "torch"),
        "XDG_CACHE_HOME": str(cache_root / "cache" / "xdg"),
    })
    return environment


@dataclass
class SearchJob:
    job_id: str
    query_sha256: str
    status: str
    created_at: float
    output_dir: Path
    log_path: Path
    process: subprocess.Popen[bytes] | None = field(default=None, repr=False)
    exit_code: int | None = None
    response_json: Path | None = None
    error: str = ""
    finished_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "query_sha256": self.query_sha256,
            "status": self.status,
            "created_at": self.created_at,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.created_at, 2),
            "exit_code": self.exit_code,
            "results_ready": bool(self.response_json and self.response_json.is_file()),
            "error": self.error,
        }


class SearchJobManager:
    def __init__(
        self,
        *,
        db_path: Path,
        output_root: Path,
        search_script: Path,
        search_config: Path,
        embedding_python: Path,
        openclip_python: Path,
    ):
        self.db_path = Path(db_path).resolve()
        self.output_root = Path(output_root).resolve()
        self.search_script = Path(search_script).resolve()
        self.search_config = Path(search_config).resolve()
        # A virtualenv's ``bin/python`` is commonly a symlink to the base
        # interpreter.  Resolving it discards the venv identity and its
        # site-packages, which is fatal when an embedded macOS app launches a
        # second model runtime.  Preserve the absolute venv entry path.
        self.embedding_python = Path(embedding_python).expanduser().absolute()
        self.openclip_python = Path(openclip_python).expanduser().absolute()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, SearchJob] = {}
        self._lock = threading.Lock()

    def _database_preflight(self) -> tuple[sqlite3.Connection | None, dict[str, Any]]:
        """Open only the selected existing task database and report why it failed.

        ``mode=ro`` prevents a search from creating an empty SQLite file when
        a task pointer is stale. A rejected shared-lock read is reported rather
        than bypassed: existence/readability alone cannot prove that a library
        has no active writer or pending WAL.
        """
        path = self.db_path
        report: dict[str, Any] = {
            "database_path": str(path),
            "database_path_absolute": path.is_absolute(),
            "database_exists": path.exists(),
            "database_is_file": path.is_file(),
            "database_readable": os.access(path, os.R_OK),
            "database_open_mode": "readonly_uri",
            "database_error": "",
        }
        if not (report["database_path_absolute"] and report["database_is_file"] and report["database_readable"]):
            report["database_error"] = "数据库文件不存在、不是普通文件或当前不可读"
            return None, report
        try:
            uri = f"file:{quote(path.as_posix())}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=5.0)
            con.execute("PRAGMA query_only=ON")
            # SQLite may create the connection before the volume rejects the
            # first shared-lock read.  Prove the selected task is genuinely
            # readable before reporting a normal readonly success.
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            return con, report
        except sqlite3.Error as uri_error:
            try:
                con.close()
            except (UnboundLocalError, AttributeError):
                pass
            report["database_open_mode"] = "readonly_uri_failed"
            report["database_uri_error"] = str(uri_error)
            report["database_error"] = str(uri_error)
            return None, report

    def readiness(self) -> dict[str, Any]:
        paths = {
            "database": self.db_path,
            "search_script": self.search_script,
            "search_config": self.search_config,
            "embedding_python": self.embedding_python,
            "openclip_python": self.openclip_python,
        }
        checks = {key: path.is_file() for key, path in paths.items()}
        database_ready = False
        text_enrichment_ready = False
        text_vector_link_coverage = False
        uncovered_video_source_count = 0
        con, database_preflight = self._database_preflight()
        checks["database"] = bool(con is not None)
        if con is not None:
            try:
                objects = {
                    str(row[0]) for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
                if 'scheduler_runs' in objects:
                    owner=con.execute('SELECT scheduler_mode FROM scheduler_runs ORDER BY execution_epoch DESC LIMIT 1').fetchone()
                    if owner and owner[0]=='event_driven_v2':
                        from .v2_search_readiness import current_text_readiness
                        con.execute('BEGIN')
                        _install_search_lineage_view(con)
                        authority=current_text_readiness(con)
                        checks.update(authority)
                        checks['database_search_ready']=all(authority.values())
                        from .v2_semantic_search import bindings
                        semantic=bool(bindings(con))
                        return {'ready':checks['database'] and checks['database_search_ready'],
                            'search_mode':'V2_CURRENT_HYBRID' if semantic else 'V2_CURRENT_TEXT','semantic_vector_search_ready':semantic,
                            'checks':checks,'uncovered_video_source_count':0,
                            'database_preflight':database_preflight}
                required = {
                    "source_assets", "derived_assets", "visual_units", "embeddings",
                    "model_runs", "visual_labels", "visual_label_terms",
                }
                database_ready = required.issubset(objects) and bool(
                    con.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone()
                )
                if database_ready:
                    visual_units = int(con.execute(
                        "SELECT COUNT(*) FROM visual_units"
                    ).fetchone()[0] or 0)
                    embedding_columns = {
                        str(row[1]) for row in con.execute("PRAGMA table_info(embeddings)")
                    }
                    # Current libraries link vectors by visual_unit_id.  Keep
                    # older visual-only libraries searchable when their
                    # one-vector-per-unit table predates that explicit column.
                    embedded_units = int(con.execute(
                        "SELECT COUNT(DISTINCT visual_unit_id) FROM embeddings"
                        if "visual_unit_id" in embedding_columns
                        else "SELECT COUNT(*) FROM embeddings"
                    ).fetchone()[0] or 0)
                    checks["visual_vector_coverage"] = (
                        visual_units > 0 and embedded_units == visual_units
                    )
                    video_sources = int(con.execute(
                        "SELECT COUNT(*) FROM source_assets WHERE media_type='video'"
                    ).fetchone()[0] or 0)
                    video_covered = int(con.execute(
                        "SELECT COUNT(DISTINCT v.source_content_id) FROM visual_units v "
                        "JOIN source_assets s USING(source_content_id) WHERE s.media_type='video'"
                    ).fetchone()[0] or 0)
                    checks["video_source_coverage"] = video_sources == video_covered
                    uncovered_video_source_count = max(0, video_sources - video_covered)
                    # Unsupported or failed source videos remain visible as a
                    # warning, but must not disable search for the complete set
                    # of successfully derived visual units.
                    database_ready = database_ready and checks["visual_vector_coverage"]
                if {
                    "stop03_5d_text_vectors", "stop03_5d_text_embedding_runs",
                }.issubset(objects):
                    text_enrichment_ready = bool(con.execute(
                        "SELECT 1 FROM stop03_5d_text_embedding_runs "
                        "WHERE status='success' LIMIT 1"
                    ).fetchone())
                    if {
                        "stop03_5d_text_documents", "stop03_5d_document_vector_links",
                    }.issubset(objects):
                        documents = int(con.execute(
                            "SELECT COUNT(*) FROM stop03_5d_text_documents"
                        ).fetchone()[0] or 0)
                        links = int(con.execute(
                            "SELECT COUNT(*) FROM stop03_5d_document_vector_links"
                        ).fetchone()[0] or 0)
                        vectors = int(con.execute(
                            "SELECT COUNT(*) FROM stop03_5d_text_vectors WHERE status='success'"
                        ).fetchone()[0] or 0)
                        text_vector_link_coverage = (
                            documents > 0 and links == documents and vectors > 0
                        )
            except (OSError, sqlite3.Error) as exc:
                database_ready = False
                database_preflight["database_error"] = str(exc)
            finally:
                con.close()
        checks["database_search_ready"] = database_ready
        checks["text_enrichment_ready"] = text_enrichment_ready
        checks["text_vector_link_coverage"] = text_vector_link_coverage
        required_checks = (
            "database", "search_script", "search_config", "embedding_python",
            "openclip_python", "database_search_ready",
        )
        hybrid_ready = text_enrichment_ready and text_vector_link_coverage
        return {
            "ready": all(checks[key] for key in required_checks),
            "search_mode": "HYBRID_VISUAL_TEXT" if hybrid_ready else "VISUAL_ONLY",
            "checks": checks,
            "uncovered_video_source_count": uncovered_video_source_count,
            "database_preflight": database_preflight,
        }

    def start(self, request: dict[str, Any]) -> dict[str, Any]:
        query = " ".join(str(request.get("query", "")).split())
        if not (1 <= len(query) <= 512):
            raise ValueError("搜索文字长度必须在 1 到 512 个字符之间")
        if self._active_scheduler_mode() == "event_driven_v2":
            with self._lock:
                if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                    raise RuntimeError("已有一个搜索正在运行，请等待它完成")
                job_id = "ui5e_" + uuid.uuid4().hex[:24]
                job_root = self.output_root / "search_jobs" / job_id
                job_root.mkdir(parents=True, exist_ok=False)
                job = SearchJob(job_id=job_id, query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    status="queued", created_at=time.time(), output_dir=job_root, log_path=job_root / "search.log")
                self._jobs[job_id] = job
            if self._run_v2_current_search(job, query, request):
                return job.public()
            with self._lock:
                self._jobs.pop(job.job_id, None)
            raise RuntimeError("V2 当前搜索投影不可用")
        readiness = self.readiness()
        if not readiness["ready"]:
            preflight = readiness.get("database_preflight", {})
            if preflight.get("database_error"):
                raise RuntimeError(
                    "搜索数据库不可用："
                    f"{preflight['database_error']}（{preflight.get('database_path', '')}）"
                )
            missing = [name for name, passed in readiness["checks"].items() if not passed]
            raise RuntimeError("搜索运行环境未通过预检：" + "、".join(missing))
        with self._lock:
            if any(job.status in {"queued", "running"} for job in self._jobs.values()):
                raise RuntimeError("已有一个搜索正在运行，请等待它完成")
            job_id = "ui5e_" + uuid.uuid4().hex[:24]
            job_root = self.output_root / "search_jobs" / job_id
            job_root.mkdir(parents=True, exist_ok=False)
            job = SearchJob(
                job_id=job_id,
                query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                status="queued",
                created_at=time.time(),
                output_dir=job_root,
                log_path=job_root / "search.log",
            )
            self._jobs[job_id] = job

        command = self.build_command(query, request, job_root / "output")
        env = offline_search_environment(job_root)
        log_file = job.log_path.open("wb")
        try:
            job.process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT, env=env)
        except Exception:
            log_file.close()
            job.status = "failed"
            raise
        job.status = "running"
        threading.Thread(target=self._wait, args=(job, log_file), daemon=True).start()
        return job.public()

    def _active_scheduler_mode(self) -> str | None:
        try:
            with sqlite3.connect(self.db_path) as con:
                # Search ownership is durable current-project ownership, not
                # worker liveness.  The newest authoritative epoch remains
                # current after COMPLETED until an explicit switch creates a
                # newer owner.
                row = con.execute(
                    "SELECT scheduler_mode FROM scheduler_runs "
                    "ORDER BY execution_epoch DESC LIMIT 1"
                ).fetchone()
                return str(row[0]) if row else None
        except sqlite3.Error:
            return None

    def _run_v2_current_search(self, job: SearchJob, query: str, request: dict[str, Any], _store=None) -> bool:
        store=_store if _store is not None else getattr(self,"session_store",None) or _RequestReadOnlyStore(self.db_path)
        if Path(store.path).resolve()!=self.db_path.resolve():raise ValueError('search_snapshot_database_mismatch')
        try:
            from .v2_index import FusionIndex
            index = FusionIndex.__new__(FusionIndex)
            index.store = store
            from .v2_index import fts_query
            from .v2_semantic_search import retrieve
            policy=json.loads(self.search_config.read_text()) if self.search_config.is_file() else {}
            from .search_evidence import EvidenceReader, allowed, mode
            search_intent=mode(request.get('search_intent'))
            semantic_scores={};semantic_details={};semantic=False
            if request.get('_semantic_preview') is not None:
                semantic_scores,semantic_details=request['_semantic_preview'];semantic=True
            self.search_channels=[]
            from .v2_release_search import enabled as publication_enabled,intent,search as evidence_search
            with index.store.connect() as con:
                speech_query=search_intent!='visual' and intent(query)[0] and publication_enabled(con)
            if policy and not speech_query and not request.get('_lexical_only') and request.get('_semantic_preview') is None:
                # Reuse the exact same durable filtering/assembly for early FTS cards.
                # This preview is not a second engine and never writes library data.
                preview=SearchJob(job_id=job.job_id+'_lexical',query_sha256='',status='running',created_at=time.time(),output_dir=job.output_dir/'lexical',log_path=job.output_dir/'lexical.log')
                if self._run_v2_current_search(preview,query,{**request,'_lexical_only':True},_store=store):
                    early=json.loads(preview.response_json.read_text())
                    import sys
                    for item in early.get('result_items',[]):
                        original=str(item.get('source_path') or '')
                        item.update(source_online=False,source_availability="unchecked",can_open_original=bool(original),original_path=original)
                    print('SEARCH_PROGRESS_JSON='+json.dumps({'contract':'media_archive_search_progress_v1','stage':'lexical_results','stage_index':2,'total_stages':7,'message':'已找到真实文字结果，继续检索语义向量','detail':'当前为部分结果；最终排序待全部通道完成','completed':None,'total':None,'elapsed_seconds':time.time()-job.created_at,'result_items':early.get('result_items',[]),'search_state':'LOADING','ordering':'provisional','update_mode':'merge_by_source'},ensure_ascii=False),file=sys.stderr,flush=True)
                def semantic_channel_ready(stage,scores,details):
                    if stage!='embedding' or not scores:return
                    preview=SearchJob(job_id=job.job_id+'_semantic_partial',query_sha256='',status='running',created_at=job.created_at,output_dir=job.output_dir/'semantic_partial',log_path=job.output_dir/'semantic_partial.log')
                    if self._run_v2_current_search(preview,query,{**request,'_semantic_preview':(scores,details)},_store=store):
                        import sys
                        early=json.loads(preview.response_json.read_text())
                        for item in early.get('result_items',[]):
                            original=str(item.get('source_path') or '')
                            item.update(source_online=False,source_availability='unchecked',can_open_original=bool(original),original_path=original)
                        print('SEARCH_PROGRESS_JSON='+json.dumps({'contract':'media_archive_search_progress_v1','stage':'semantic_partial_results','stage_index':4,'total_stages':7,'message':'已有文本语义结果，继续补充画面匹配','detail':'当前为部分结果；最终排序待全部通道完成','elapsed_seconds':time.time()-job.created_at,'result_items':early.get('result_items',[]),'search_state':'LOADING','ordering':'provisional','update_mode':'merge_by_source'},ensure_ascii=False),file=sys.stderr,flush=True)
                semantic_scores,semantic_details,semantic=retrieve(index.store,query,
                    embedding_python=self.embedding_python,openclip_python=self.openclip_python,policy=policy,
                    query_cache=getattr(self,'request_query_cache',None),on_channel=semantic_channel_ready,search_intent=search_intent)
            with index.store.connect() as con:
                if speech_query:
                    rows=[dict(document_id=h['segment_id'],source_id=h['source_id'],source_revision=h['source_revision'],
                        artifact_id=h['artifact_id'],start_ms=h['start_ms'],end_ms=h['end_ms'],document_type='speech_evidence',
                        text='有人声',rank=-h['score'],speech_evidence=h)
                        for h in evidence_search(con,query,limit=500)]
                else:
                    rows = [dict(row) for row in con.execute("""SELECT d.*,bm25(v2_text_fts) rank
                    FROM v2_text_fts f JOIN v2_searchable_documents d ON d.document_id=f.document_id
                    JOIN v2_current_jobs j ON j.execution_key=d.raw_result_ref
                    WHERE v2_text_fts MATCH ? AND j.status='DONE'
                    AND NOT EXISTS(SELECT 1 FROM v2_active_sources a WHERE a.source_id=d.source_id AND a.source_revision<>d.source_revision)
                    ORDER BY rank,f.document_id""", (fts_query(query),))]
                if not speech_query:rows=_published_filename_rows(con,query)+rows
                from .v2_release_search import filter_rows
                rows=filter_rows(con,rows)
                evidence_reader=EvidenceReader(con)
                rows=[evidence_reader.classify(row) for row in rows]
                from .search_evidence import direct_lexical_rows
                rows=direct_lexical_rows(rows,query,search_intent)
                rows=[row for row in rows if allowed(row,search_intent)]
                lexical_rows=list(rows)
                from .build150_search_bindings import bindings as search_bindings
                vector_namespaces=[] if request.get("_early_text_preview") else [ns for _,ns,_,_ in search_bindings(con)]
                self.search_channels=[{'name':name,'available':True,'requested':requested,'used':used,'hit_count':count,'error':None} for name,requested,used,count in [('Speech',bool(speech_query),bool(speech_query),len(rows) if speech_query else 0),('FTS',not speech_query,not speech_query,len(rows) if not speech_query else 0),('Text Vector',not speech_query,bool(semantic and any(ns.startswith('text:') for ns in vector_namespaces)),sum(any(r.get('type')=='TEXT_SEMANTIC' for r in ds) for ds in semantic_details.values())),('Visual Vector',not speech_query,bool(semantic and any(ns.startswith('visual:') for ns in vector_namespaces)),sum(any(r.get('type')=='VISUAL_SEMANTIC' for r in ds) for ds in semantic_details.values())),('Fusion',True,True,len(rows)+len(semantic_scores))]]
                for name,types in [('OCR / AI Description (final_visual)',{'final_visual','ocr','qwen'}),('Transcript',{'audio','whisper','transcript'})]:
                    self.search_channels.append({'name':name,'available':True,'requested':not speech_query,'used':not speech_query,'hit_count':sum(r['document_type'] in types for r in lexical_rows),'error':None})
                for channel in self.search_channels:
                    if channel['name'] in ('Text Vector','Visual Vector'):channel['available']=any(ns.startswith('text:' if channel['name']=='Text Vector' else 'visual:') for ns in vector_namespaces)
                self.search_channels.append({'name':'Metadata','available':True,'requested':True,'used':True,'hit_count':len(rows),'error':None})
                seen={r['document_id'] for r in rows}
                for rank,row in enumerate(rows,1):
                    row['lexical_match']=True
                    row['rank']=-max(1/(policy.get('rrf_k',60)+rank),semantic_scores.get(row['document_id'],0))
                for entity,score in semantic_scores.items():
                    if entity in seen:continue
                    doc=con.execute('SELECT * FROM v2_searchable_documents WHERE document_id=?',(entity,)).fetchone()
                    if doc:
                        row=dict(doc)
                    else:
                        artifact=con.execute('SELECT * FROM v2_artifacts WHERE artifact_id=?',(entity,)).fetchone()
                        if not artifact:continue
                        row={'document_id':entity,'source_id':artifact['source_id'],'source_revision':artifact['revision'],
                            'artifact_id':entity,'start_ms':artifact['time_ms'],'document_type':'visual','text':''}
                    row=evidence_reader.classify(row)
                    if not allowed(row,search_intent):continue
                    if row.get('document_type')=='final_visual' and row['evidence_category']=='visual_description':
                        row.update(evidence_category='expanded_visual_clue',evidence_text=row.get('text',''),evidence_is_direct=False)
                    row['lexical_match']=False;row['rank']=-score;rows.append(row)
                rows=filter_rows(con,rows)
                rows.sort(key=lambda r:(r['rank'],r['document_id']))
                objects = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                ocr_sources = {(r[0], r[1]) for r in con.execute("""SELECT source_id,source_revision
                    FROM v2_current_jobs WHERE stage='ocr' AND status='DONE'
                    AND length(trim(COALESCE(json_extract(result_json,'$.text'),'')))>0""")} if request.get('has_ocr') else set()
                person_sources = {(r[0], r[1]) for r in con.execute('''SELECT o.source_id,o.source_revision
                    FROM v2_person_observations o JOIN v2_observation_heads h
                    ON h.observation_id=o.observation_id''')} if request.get('has_person') and 'v2_person_observations' in objects else set()
                if request.get('has_person'):
                    from .person_library_repair import read_frames
                    repaired=read_frames(self.db_path)
                    if repaired is not None:
                        for frame in repaired:
                            revision=con.execute('SELECT source_revision FROM v2_active_sources WHERE source_id=?',(frame['source_content_id'],)).fetchone()
                            if revision:person_sources.add((frame['source_content_id'],revision[0]))
                metadata = {}
                artifacts = {}
                for row in rows:
                    key = (row['source_id'], row['source_revision'])
                    if key not in metadata:
                        meta = con.execute('SELECT path,metadata FROM v2_source_metadata WHERE source_id=? AND source_revision=?', key).fetchone() if 'v2_source_metadata' in objects else None
                        metadata[key] = dict(meta) if meta else {}
                    aid = row.get('artifact_id')
                    if aid not in artifacts:
                        artifact = con.execute('SELECT path FROM v2_artifacts WHERE artifact_id=?', (aid,)).fetchone() if 'v2_artifacts' in objects else None
                        from .library_path_alias import resolve
                        artifacts[aid] = resolve(self.db_path,artifact[0]) if artifact else None
                    meta = metadata[key]
                    row.update(source_path=meta.get('path'), metadata=meta.get('metadata'), preview_path=artifacts[aid])
                    if publication_enabled(con) and not row.get('speech_evidence') and json.loads(meta.get('metadata') or '{}').get('media_type')=='video':
                        segment=con.execute('''SELECT s.evidence FROM release_search_segments s JOIN release_search_heads h
                            ON h.source_id=s.source_id AND h.revision=s.revision AND h.generation=s.generation AND h.visible=1
                            WHERE s.source_id=? AND s.revision=? AND (? IS NULL OR s.artifact_id=?)
                            ORDER BY abs(s.start_ms-?) LIMIT 1''',(row['source_id'],row['source_revision'],aid,aid,row.get('start_ms') or 0)).fetchone()
                        if segment:
                            row['speech_evidence']=json.loads(segment[0])
                            row['preview_path']=row['preview_path'] or row['speech_evidence'].get('preview_path')
            eligible = []
            for row in rows:
                meta = json.loads(row['metadata'] or '{}')
                kind = meta.get('media_type', meta.get('media_kind', 'image'))
                requested = request.get('media_type', 'all')
                audio = row.get('document_type') in {'audio', 'whisper', 'transcript'}
                if requested == 'audio' and not audio:
                    continue
                if requested != 'audio' and audio and not (row.get('speech_evidence') and kind=='video'):
                    continue
                if requested in {'image', 'video'} and kind != requested:
                    continue
                prefix = str(request.get('path_prefix') or '')
                if prefix and not str(meta.get('source_relative_path') or '').startswith(prefix):
                    continue
                mtime = meta.get('source_mtime', None)
                if mtime is None and meta.get('source_mtime_ns') is not None:
                    mtime = int(meta['source_mtime_ns']) / 1_000_000_000
                if any(request.get(bound) is not None and (mtime is None or
                       (mtime < int(request[bound]) if bound.endswith('min') else mtime > int(request[bound])))
                       for bound in ('source_mtime_min', 'source_mtime_max')):
                    continue
                if request.get('has_ocr') and (row['source_id'], row['source_revision']) not in ocr_sources:
                    continue
                if request.get('has_person') and (row['source_id'], row['source_revision']) not in person_sources:
                    continue
                eligible.append(row)
            # Counts describe real eligible entities, not sum-of-channel duplicates.
            eligible_ids={r['document_id'] for r in eligible}
            for channel in self.search_channels:
                name=channel['name']
                if name in ('FTS','Speech'):
                    channel['hit_count']=sum(bool(r.get('lexical_match')) for r in eligible) if channel['used'] else 0
                elif name=='Fusion':channel['hit_count']=len(eligible_ids)
                elif name=='Metadata':channel['hit_count']=sum(r['evidence_category']=='filename' for r in eligible)
                elif name=='Transcript':channel['hit_count']=sum(r['evidence_category']=='dialogue' for r in eligible)
                elif name=='OCR / AI Description (final_visual)':channel['hit_count']=sum(r['evidence_category'] in {'ocr','visual_description','expanded_visual_clue'} for r in eligible)
                elif name in ('Text Vector','Visual Vector'):
                    kind='TEXT_SEMANTIC' if name=='Text Vector' else 'VISUAL_SEMANTIC'
                    channel['hit_count']=sum(eid in eligible_ids and any(d.get('type')==kind for d in ds) for eid,ds in semantic_details.items())
                    channel['used']=bool(semantic and not speech_query and (name=='Text Vector' or search_intent!='text'))

            grouped = source_results({"result_id": row["document_id"], "source_content_id": row["source_id"],
                "text_preview": row.get("evidence_text",row["text"]), "time_position_ms": row.get("start_ms"),
                "source_revision":row["source_revision"],"evidence_category":row["evidence_category"],
                "evidence_direct_description":row.get("evidence_direct_description"),"lexical_match":row.get("lexical_match",False),
                "evidence_document_id":row["evidence_document_id"],"search_intent":search_intent,
                "source_path": row["source_path"], "preview_path": row["preview_path"],
                "source_relative_path": json.loads(row['metadata'] or '{}').get('source_relative_path',''),
                "media_type": json.loads(row["metadata"] or "{}").get("media_type", "image"),
                "visual_unit_id": row.get("artifact_id"), "score": -float(row["rank"]),
                "speech_evidence":row.get('speech_evidence'),
                "result_start_ms":row.get('start_ms'),"result_end_ms":row.get('end_ms'),
                "match_reasons": ([{"type": {"visual_description":"VISUAL_DESCRIPTION", "expanded_visual_clue":"EXPANDED_KEYWORD", "dialogue":"TRANSCRIPT", "ocr":"OCR", "filename":"SOURCE_FILENAME"}.get(row['evidence_category'],"CONTEXT_CLUE"),
                    "authority":"current_published_text","text":row.get('evidence_text',row['text']),"document_id":row['document_id']}]
                    if row.get('lexical_match') else []) + semantic_details.get(row['document_id'],[])
            } for row in eligible)
            output = job.output_dir / "query5ev2_current" / "reports" / "search_results.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": "PASS", "technical_status": "PASS", "policy_status": "PASS",
                "runtime": {"search_truth": "v2_current_projection"},
                "search_mode":"V2_CURRENT_HYBRID" if semantic else "V2_CURRENT_TEXT",
                "semantic_vector_search_ready":semantic,"model_run":semantic,"search_intent":search_intent,
                "search_result_contract": "media_archive_source_search_v2", "search_state": "COMPLETE",
                "result_count_by_media": {kind:sum(row.get('media_type')==kind for row in grouped) for kind in ('image','video')},
                **page(grouped, request.get("offset", 0)),
                # Send all assembled source results once; the existing UI buffers/render-pages them.
                "result_items":grouped[max(0,int(request.get("offset",0))):],
                "result_limit":len(grouped),"next_result_offset":None,
            }
            output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            job.response_json = output; job.exit_code = 0; job.status = "success"; job.finished_at = time.time()
            return True
        except (sqlite3.Error, ValueError, KeyError, RuntimeError, OSError) as error:
            job.failure={'error_code':type(error).__name__,'error_message':str(error),'failed_component':'query_execution','failed_path':getattr(error,'filename',None),'recoverable':True}
            job.error = f'搜索未完成：{type(error).__name__}: {error}'
            job.log_path.write_text(job.error, encoding='utf-8')
            return False
        finally:
            if _store is None and store is not getattr(self,"session_store",None):store.close()

    def build_command(self, query: str, request: dict[str, Any], output: Path) -> list[str]:
        limit = PAGE_SIZE
        offset = max(0, int(request.get("offset", 0)))
        temporal = max(0, min(int(request.get("temporal_dedup_ms", 5000)), 60000))
        preview = int(request.get("preview_window_ms", 10000))
        preview = preview if preview in {5000, 10000} else 10000
        command = [
            str(self.embedding_python), str(self.search_script),
            "--mode", "query", "--db", str(self.db_path),
            "--config", str(self.search_config), "--out", str(output),
            "--query", query, "--openclip-python", str(self.openclip_python),
            "--result-offset", str(offset), "--result-limit", str(limit),
            "--temporal-dedup-ms", str(temporal), "--preview-window-ms", str(preview),
            "--timecode-precision", "millisecond", "--device", str(request.get("device", "auto")),
            "--confirm-real-local-query", "--native-app-result-contract",
            "--native-readiness-verified",
        ]
        media_type = str(request.get("media_type", "all"))
        if media_type in {"image", "video"}:
            command.extend(["--media-type", media_type])
            command.append("--disable-audio-evidence")
        elif media_type == "audio":
            # Speech evidence is retained as text attached to its source video,
            # not as a standalone audio asset.
            command.extend(["--media-type", "video", "--audio-evidence-only"])
        else:
            # "all" is all visible media, not every internal evidence channel.
            command.append("--disable-audio-evidence")
        prefix = " ".join(str(request.get("path_prefix", "")).split())
        if prefix:
            command.extend(["--source-relative-path-prefix", prefix])
        for key, option in (
            ("source_mtime_min", "--source-mtime-min"),
            ("source_mtime_max", "--source-mtime-max"),
        ):
            if request.get(key) is not None:
                command.extend([option, str(int(request[key]))])
        if request.get("has_ocr"):
            command.append("--has-ocr")
        if request.get("has_person"):
            command.append("--has-person")
        return command

    def _wait(self, job: SearchJob, log_file: Any) -> None:
        try:
            assert job.process is not None
            job.exit_code = job.process.wait()
        finally:
            log_file.close()
            job.finished_at = time.time()
        candidates = sorted(job.output_dir.glob("query5ev2_*/reports/search_results.json"))
        if job.exit_code == 0 and len(candidates) == 1:
            job.response_json = candidates[0]
            job.status = "success"
        else:
            job.status = "failed"
            job.error = f"搜索退出码 {job.exit_code}，结果文件数量 {len(candidates)}"

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public() if job else None

    def results(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            path = job.response_json if job else None
        if not path or not path.is_file():
            candidates = sorted(job.output_dir.glob("**/reports/search_progress_results.json")) if job else []
            if len(candidates) != 1:
                return None
            path = candidates[0]
        payload = json.loads(path.read_text(encoding="utf-8"))
        public_results = []
        for row in payload.get("result_items", payload.get("results", [])):
            keep = {
                key: row.get(key) for key in (
                    "result_id", "source_content_id", "visual_unit_id", "derived_id",
                    "source_relative_path", "media_type", "frame_index", "time_position_ms",
                    "timecode", "preview_segment_start_ms", "preview_segment_end_ms",
                    "preview_segment_start_timecode", "preview_segment_end_timecode",
                    "hybrid_score", "openclip_cosine", "text_semantic_score", "text_evidence_present",
                    "text_preview", "yoloe_labels", "environment_label",
                    "environment_user_confirmation_required",
                    "audio_transcript_match", "audio_evidence_id", "audio_start_time_ms",
                    "audio_end_time_ms", "audio_hit_time_ms", "source_id", "source_type", "original_path", "display_name",
                    "representative_thumbnail", "top_match", "other_match_count", "match_clusters",
                    "other_match_clusters", "match_reasons", "final_score", "relevance_reasons",
                    "matched_object_labels", "source_match_count", "evidence_summary", "fusion_policy", "source_revision",
                )
            }
            keep["preview_url"] = f"/api/preview/{row.get('derived_id')}"
            if row.get("media_type") == "video":
                keep["media_url"] = f"/api/media/{row.get('source_content_id')}"
            public_results.append(keep)
        return {
            "search_result_contract": payload.get("search_result_contract", payload.get("contract")),
            "progress": {key: payload.get(key) for key in (
                "search_state", "generation", "scanned_count", "total_count", "current_source_result_count", "ordering", "update_mode")},
            "job": job.public() if job else None,
            "status": payload.get("status"),
            "technical_status": payload.get("technical_status", payload.get("status")),
            "policy_status": payload.get("policy_status"),
            "runtime": payload.get("runtime", {}),
            "coverage": {
                "eligible_visual_unit_count": payload.get("eligible_visual_unit_count", 0),
                "scanned_visual_vector_count": payload.get("scanned_visual_vector_count", 0),
                "scanned_text_vector_count": payload.get("scanned_text_vector_count", 0),
            },
            "ranking": payload.get("ranking", {
                "post_temporal_dedup_result_count": payload.get("result_total_count", 0),
                "result_offset": payload.get("result_offset", 0),
                "result_limit": payload.get("result_limit", 0),
                "next_result_offset": payload.get("next_result_offset"),
            }),
            "results": public_results,
        }

    def stop_all(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.process and job.process.poll() is None:
                job.process.terminate()
