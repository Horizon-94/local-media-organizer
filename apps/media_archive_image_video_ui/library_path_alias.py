"""Resolve copied derived previews without rewriting hashed historical evidence."""
import json
from pathlib import Path

def resolve(database, value):
    if not value: return value
    workspace = Path(database).resolve().parent
    manifest = workspace / 'path_aliases.json'
    if not manifest.is_file(): return str(value)
    binding = json.loads(manifest.read_text())
    if binding.get('contract') != 'derived_workspace_alias_v1':
        raise ValueError('derived_workspace_alias_contract_invalid')
    old = Path(binding['source_workspace'])
    if not old.is_absolute() or old.name != 'workspace':
        raise ValueError('derived_workspace_alias_root_invalid')
    path = Path(str(value))
    try: relative = path.relative_to(old)
    except ValueError: return str(value)
    target = (workspace / relative).resolve()
    if not target.is_relative_to(workspace): raise ValueError('derived_workspace_alias_escape')
    return str(target)
