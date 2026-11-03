"""Read-only image inventory. No decoder or model is imported by discovery."""
from __future__ import annotations
from collections import Counter, defaultdict
import ast
import hashlib
import json
from pathlib import Path
import subprocess

REPO=Path(__file__).resolve().parents[2]
def literal_sets(path,names):
    result={}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node,ast.Assign):
            for target in node.targets:
                if isinstance(target,ast.Name) and target.id in names:result[target.id]=ast.literal_eval(node.value)
    if set(result)!=set(names):raise ValueError('FROZEN_FORMAT_CONTRACT_NOT_FOUND')
    return result
# One format authority, not a second hand-maintained decoder whitelist. AST
# reads constants only; importing discovery never executes a legacy script.
_preview=literal_sets(REPO/'scripts/02_step01_step02_pipeline/step02_2_image_preview_from_db_safe_v6_20260709_182200.py',('SIPS_EXTS','QL_EXTS'))
_scanner=literal_sets(REPO/'scripts/02_step01_step02_pipeline/step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py',('IMAGE_EXTS','VIDEO_EXTS','AUDIO_EXTS','METADATA_SIDECAR_EXTS'))
IMAGES=_preview['SIPS_EXTS']|_preview['QL_EXTS']
DISCOVERED_IMAGES=_scanner['IMAGE_EXTS']|{'.gpr'}  # GPR exists here, NOT enabled
SIDECARS=_scanner['METADATA_SIDECAR_EXTS']
VIDEO_AUDIO=_scanner['VIDEO_EXTS']|_scanner['AUDIO_EXTS']

def identity(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def inventory(root,progress=None):
    root=Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():raise ValueError('SOURCE_ROOT_NOT_DIRECTORY')
    files=[];warnings=[];families=defaultdict(list);formats=defaultdict(list)
    paths=[]
    for path in root.rglob('*'):
        paths.append(path)
        if progress:progress('DISCOVERING_SOURCES',entries_discovered=len(paths),current_source=str(path.relative_to(root)))
    discovered_bytes=0
    for path in sorted(paths):
        if path.is_symlink():
            warnings.append({'path':str(path.relative_to(root)),'reason':'SYMLINK_NOT_FOLLOWED'});continue
        if not path.is_file() or path.name.startswith('._'):continue
        st=path.stat();relative=str(path.relative_to(root));ext=path.suffix.lower()
        kind=('image' if ext in IMAGES else 'unsupported_image' if ext in DISCOVERED_IMAGES
              else 'sidecar' if ext in SIDECARS else 'filtered_video_audio' if ext in VIDEO_AUDIO else 'other')
        row={'relative_path':relative,'path':str(path),'extension':ext,'bytes':st.st_size,
             'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,'media_kind':kind,
             'source_id':identity(('image-source-path-v1',str(root),relative)),
             'family_id':identity(('folder-casefold-stem-v1',str(path.parent),path.stem.casefold()))}
        files.append(row);formats[ext].append(row)
        discovered_bytes+=st.st_size
        if progress:progress('DISCOVERING_SOURCES',source_count_discovered=len(files),total_source_bytes=discovered_bytes,current_source=relative)
        if kind in ('image','sidecar','unsupported_image'):families[row['family_id']].append(row)
    summaries={}
    for ext,rows in sorted(formats.items()):
        example=rows[0]['path']
        probe=subprocess.run(['/usr/bin/file','--mime-type','-b',example],capture_output=True,text=True,timeout=15)
        summaries[ext]={'count':len(rows),'bytes':sum(x['bytes'] for x in rows),
                        'sample_mime':probe.stdout.strip() if probe.returncode==0 else 'UNKNOWN',
                        'mime_scope':'one actual sample, not every file','sample':example}
    family_rows=[]
    for fid,rows in families.items():
        if len(rows)>1:
            family_rows.append({'family_id':fid,'members':[r['relative_path'] for r in rows],
                                'images':[r['source_id'] for r in rows if r['media_kind']=='image'],
                                'sidecars':[r['relative_path'] for r in rows if r['media_kind']=='sidecar'],
                                'relationship':'SIBLINGS_NOT_CONTENT_EQUIVALENCE'})
    return {'contract':'image-inventory-readonly-v1','source_root':str(root),'files':files,
            'file_count':len(files),'total_bytes':sum(r['bytes'] for r in files),
            'counts':dict(Counter(r['media_kind'] for r in files)), 'extensions':summaries,
            'directories':dict(Counter(str(Path(r['relative_path']).parent) for r in files)),
            'families':family_rows,'warnings':warnings,
            'inventory_sha':identity([(r['relative_path'],r['bytes'],r['mtime_ns'],r['inode']) for r in files])}

def storage_preflight(source,workspace,inv):
    import shutil
    source=Path(source).resolve();workspace=Path(workspace).resolve()
    if workspace==source or source in workspace.parents or workspace in source.parents:
        raise ValueError('WORKSPACE_SOURCE_OVERLAP')
    anchor=workspace
    while not anchor.exists():anchor=anchor.parent
    image_count=inv['counts'].get('image',0)
    # Conservative reservation, not a promise about actual compressed bytes.
    # Preview<=8MiB + business DB/index<=8MiB + diagnostics<=1MiB per source;
    # 4GiB safety. Source originals never copied.
    preview_count=inv.get('preview_required',image_count)
    required=preview_count*8*1024**2+image_count*9*1024**2+4*1024**3
    free=shutil.disk_usage(anchor).free
    def volume(path):
        p=subprocess.run(['/usr/sbin/diskutil','info','-plist',str(path)],capture_output=True,timeout=15)
        import plistlib
        data=plistlib.loads(p.stdout) if p.returncode==0 else {}
        result={k:data.get(k) for k in ('MountPoint','FilesystemType','VolumeName','Internal','DeviceIdentifier')}
        if not data:
            # DiskManagement can be unavailable in a sandbox. Report mount/FS
            # from the kernel mount table, never invent a disk classification.
            output=subprocess.run(['/sbin/mount'],capture_output=True,text=True,timeout=10).stdout
            df=subprocess.run(['/bin/df','-k',str(path)],capture_output=True,text=True,timeout=10)
            actual_mount=None
            if df.returncode==0 and len(df.stdout.splitlines())>1:
                fields=df.stdout.splitlines()[-1].split()
                if fields:actual_mount=fields[-1]
            matches=[]
            for line in output.splitlines():
                if ' on ' not in line or ' (' not in line:continue
                device,rest=line.split(' on ',1);mount,options=rest.rsplit(' (',1)
                if mount==actual_mount or not actual_mount and (Path(path)==Path(mount) or Path(mount) in Path(path).parents):
                    matches.append((len(mount),device,mount,options))
            if matches:
                _,device,mount,options=max(matches)
                result.update(MountPoint=mount,FilesystemType=options.split(',')[0].rstrip(')'),DeviceIdentifier=device)
            result['probe_warning']='diskutil unavailable; df resolves APFS firmlink mount; Internal/VolumeName UNKNOWN'
        return result
    return {'source_volume':volume(source),'workspace_volume':volume(anchor),
            'workspace_reason':'isolated writable local work area; originals stay in place',
            'required_free_bytes':required,'free_bytes':free,'enough_space':free>=required,
            'components':{'preview_count':preview_count,'preview_bytes_per_image':8*1024**2,'db_index_bytes_per_image':8*1024**2,
                          'telemetry_bytes_per_image':1024**2,'safety_bytes':4*1024**3}}
