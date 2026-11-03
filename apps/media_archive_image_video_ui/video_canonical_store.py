"""Isolated immutable result/provenance store and pinned selection proofs.

Prepare performs JSON work outside the writer. SQL proposals contain only
encoded immutable bodies/references. Existing image/legacy schemas untouched.
"""
from dataclasses import dataclass
import hashlib,json,time,zlib
from .video_commit_queue import SQLCommand,WriteProposal


def encode(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()
def sha(raw):return hashlib.sha256(raw).hexdigest()


SCHEMA='''
PRAGMA user_version=3;
CREATE TABLE canonical_result(result_id TEXT PRIMARY KEY,result_sha TEXT NOT NULL UNIQUE,
 stage TEXT NOT NULL,schema_version TEXT NOT NULL,body BLOB NOT NULL,body_sha TEXT NOT NULL,created_at REAL NOT NULL,
 registry_id TEXT REFERENCES yolo_registry(registry_id));
CREATE TABLE yolo_registry(registry_id TEXT PRIMARY KEY,registry_sha TEXT NOT NULL UNIQUE,
 model_version TEXT NOT NULL,body BLOB NOT NULL);
CREATE TABLE immutable_payload(payload_id TEXT PRIMARY KEY,kind TEXT NOT NULL,body BLOB NOT NULL);
CREATE TABLE evidence_header(execution_key TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,
 stage TEXT,status TEXT,attempts INTEGER,result_sha TEXT,result_id TEXT REFERENCES canonical_result(result_id),
 provenance_ref TEXT REFERENCES immutable_payload(payload_id),payload_ref TEXT REFERENCES immutable_payload(payload_id),input_sha TEXT NOT NULL);
CREATE TABLE selection_proof(proof_id TEXT PRIMARY KEY,source_id TEXT,source_revision TEXT,
 policy_version TEXT,manifest_sha TEXT,evidence_set_sha TEXT,created_at REAL);
CREATE TABLE proof_evidence(proof_id TEXT,evidence_key TEXT,evidence_sha TEXT,ordinal INTEGER,
 PRIMARY KEY(proof_id,evidence_key),FOREIGN KEY(proof_id) REFERENCES selection_proof(proof_id),
 FOREIGN KEY(evidence_key) REFERENCES evidence_header(execution_key));
CREATE TABLE candidate_proof(candidate_key TEXT PRIMARY KEY REFERENCES evidence_header(execution_key),proof_id TEXT REFERENCES selection_proof(proof_id));
CREATE TABLE result_reference(kind TEXT,identity TEXT PRIMARY KEY,result_id TEXT REFERENCES canonical_result(result_id));
CREATE TABLE active_source(source_id TEXT PRIMARY KEY,source_revision TEXT);
'''


@dataclass(frozen=True)
class PreparedResult:
    result_id: str
    result_sha: str
    proposal: WriteProposal
    dynamic_bytes: int


def prepare_result(stage,schema_version,value,*,source='',model_version='',storage_value=None):
    original=encode(value);result_sha=sha(original)
    # Immutable body identity is independent of the consuming stage contract;
    # those contracts remain on the evidence/job header and provenance.
    result_id=result_sha;commands=[];stored=value if storage_value is None else storage_value;registry_id=None
    if stage=='yoloe' and 'registry' in value and 'label_terms' in value:
        static=encode({'registry':value['registry'],'label_terms':value['label_terms']})
        registry_sha=sha(static);registry_id=registry_sha
        commands.append(SQLCommand('INSERT OR IGNORE INTO yolo_registry VALUES(?,?,?,?)',
            (registry_id,registry_sha,model_version,zlib.compress(static))))
        stored={k:v for k,v in value.items() if k not in ('registry','label_terms')}
        stored={**stored,'$registry_ref':registry_id}
    body=encode(stored)
    commands.append(SQLCommand('INSERT OR IGNORE INTO canonical_result VALUES(?,?,?,?,?,?,?,?)',
        (result_id,result_sha,stage,schema_version,zlib.compress(body),sha(body),time.time(),registry_id)))
    return PreparedResult(result_id,result_sha,WriteProposal('canonical_result',source,tuple(commands)),len(body))


def load_result(con,result_id,_seen=None):
    seen=set() if _seen is None else _seen
    if result_id in seen or len(seen)>=128:raise ValueError('canonical_reference_cycle')
    seen.add(result_id)
    row=con.execute('SELECT result_sha,body,body_sha FROM canonical_result WHERE result_id=?',(result_id,)).fetchone()
    if not row:raise ValueError('canonical_result_missing')
    raw=zlib.decompress(row[1])
    if sha(raw)!=row[2]:raise ValueError('canonical_body_corrupt')
    value=json.loads(raw)
    if isinstance(value,dict) and '$registry_ref' in value:
        ref=value.pop('$registry_ref')
        registry=con.execute('SELECT registry_sha,body FROM yolo_registry WHERE registry_id=?',(ref,)).fetchone()
        if not registry:raise ValueError('canonical_registry_missing')
        static=zlib.decompress(registry[1])
        if sha(static)!=registry[0]:raise ValueError('canonical_registry_corrupt')
        value.update(json.loads(static))
    def restore(node):
        if isinstance(node,dict):
            if set(node)=={'$canonical_result'}:return load_result(con,node['$canonical_result'],seen)
            return {key:restore(item) for key,item in node.items()}
        if isinstance(node,list):return [restore(item) for item in node]
        return node
    value=restore(value)
    if sha(encode(value))!=row[0]:raise ValueError('canonical_result_semantics_changed')
    seen.remove(result_id)
    return value


def payload_command(kind,value):
    raw=encode(value);key=sha(raw)
    return key,SQLCommand('INSERT OR IGNORE INTO immutable_payload VALUES(?,?,?)',(key,kind,zlib.compress(raw)))


def prepare_proof(proof_id,source,revision,policy,manifest,evidence):
    ordered=sorted(set(tuple(x) for x in evidence));digest=sha(encode(ordered))
    commands=[SQLCommand('INSERT OR IGNORE INTO selection_proof VALUES(?,?,?,?,?,?,?)',
        (proof_id,source,revision,policy,manifest,digest,time.time()))]
    commands.extend(SQLCommand('INSERT OR IGNORE INTO proof_evidence VALUES(?,?,?,?)',
        (proof_id,key,value,index)) for index,(key,value) in enumerate(ordered))
    return WriteProposal('selection_proof',source,tuple(commands))


def proof_current(con,proof_id,source,revision):
    active=con.execute('SELECT source_revision FROM active_source WHERE source_id=?',(source,)).fetchone()
    if not active or active[0]!=revision:return False
    proof=con.execute('SELECT evidence_set_sha FROM selection_proof WHERE proof_id=? AND source_id=? AND source_revision=?',
        (proof_id,source,revision)).fetchone()
    if not proof:return False
    rows=con.execute('SELECT evidence_key,evidence_sha FROM proof_evidence WHERE proof_id=? ORDER BY ordinal',(proof_id,)).fetchall()
    if sha(encode([tuple(r) for r in rows]))!=proof[0]:return False
    return con.execute('''SELECT 1 FROM proof_evidence p LEFT JOIN evidence_header h ON h.execution_key=p.evidence_key
      WHERE p.proof_id=? AND (h.execution_key IS NULL OR h.status<>'DONE' OR h.result_sha<>p.evidence_sha
        OR h.source_id<>? OR h.source_revision<>?) LIMIT 1''',(proof_id,source,revision)).fetchone() is None
