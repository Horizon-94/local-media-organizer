"""VIDEO-only dual reader: legacy JSON and immutable canonical references.

No writer activation or migration here. This boundary preserves the JSON
business objects expected by existing policy readers while the physical ledger
may hold references. Missing/corrupt bodies fail closed, never become {}.
"""
import json
import sqlite3
from .video_canonical_store import load_result

REFERENCE='$canonical_result'
JSON_COLUMNS=frozenset(('result_json','payload','input_json','error_message','provenance','artifact_json','segments_json','row_json'))


def reference(result_id):
    return {REFERENCE:result_id}


def hydrate(connection,value):
    if isinstance(value,dict):
        if set(value)=={REFERENCE}:
            if not isinstance(value[REFERENCE],str):raise ValueError('canonical_reference_invalid')
            return load_result(connection,value[REFERENCE])
        return {key:hydrate(connection,item) for key,item in value.items()}
    if isinstance(value,list):return [hydrate(connection,item) for item in value]
    return value


def read_json(connection,value):
    decoded=json.loads(value) if isinstance(value,(str,bytes)) else value
    return hydrate(connection,decoded)


class BusinessRow:
    """Request-local lazy compatibility; no process/global value cache."""
    def __init__(self,connection,cursor,values):
        self.connection=connection
        self.names=tuple(column[0] for column in cursor.description)
        self.values=values

    def keys(self):return list(self.names)
    def __len__(self):return len(self.values)
    def __iter__(self):return (self[index] for index in range(len(self)))
    def __getitem__(self,key):
        index=self.names.index(key) if isinstance(key,str) else key
        if isinstance(index,slice):return tuple(self[i] for i in range(*index.indices(len(self))))
        value=self.values[index]
        if (self.names[index] in JSON_COLUMNS and isinstance(value,str)
                and REFERENCE in value):
            if self.names[index]=='error_message' and not value.startswith('{'):return value
            # Legacy string conventions (including ensure_ascii) matter for
            # existing fingerprint/provenance readers. Restore those exactly.
            restored=read_json(self.connection,value)
            if self.names[index]=='error_message' and isinstance(restored,str):return restored
            return json.dumps(restored,sort_keys=True)
        return value


def row_factory(cursor,values):
    return BusinessRow(cursor.connection,cursor,values)
