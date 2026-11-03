"""Lossless, immutable sharing of large planning arrays. Old JSON remains readable."""
import hashlib
import json
import zlib

MARKER = '$v2_shared_planning_v1'
FIELDS = {'source_assets', 'finder_tags'}

def dump(c, value):
    c.execute('CREATE TABLE IF NOT EXISTS v2_shared_planning (digest TEXT PRIMARY KEY, codec TEXT NOT NULL, payload BLOB NOT NULL)')
    def pack(node):
        if isinstance(node, dict):
            result = {}
            for key, item in node.items():
                raw = json.dumps(item, sort_keys=True).encode('utf-8') if key in FIELDS and isinstance(item, list) else b''
                if len(raw) >= 4096:
                    digest = hashlib.sha256(raw).hexdigest()
                    c.execute('INSERT OR IGNORE INTO v2_shared_planning VALUES(?,?,?)', (digest, 'zlib-json-v1', zlib.compress(raw)))
                    result[key] = {MARKER: digest}
                else:
                    result[key] = pack(item)
            return result
        if isinstance(node, list): return [pack(item) for item in node]
        return node
    return json.dumps(pack(value), sort_keys=True)

def load(c, text):
    def unpack(node):
        if isinstance(node, dict):
            if set(node) == {MARKER}:
                row = c.execute('SELECT codec,payload FROM v2_shared_planning WHERE digest=?', (node[MARKER],)).fetchone()
                if not row or row[0] != 'zlib-json-v1': raise ValueError('shared_planning_missing_or_codec_invalid')
                raw = zlib.decompress(row[1])
                if hashlib.sha256(raw).hexdigest() != node[MARKER]: raise ValueError('shared_planning_digest_invalid')
                return json.loads(raw)
            return {key: unpack(item) for key, item in node.items()}
        if isinstance(node, list): return [unpack(item) for item in node]
        return node
    return unpack(json.loads(text))
