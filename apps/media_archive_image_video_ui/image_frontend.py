"""IMAGE-only frontend. Frozen A9T rules; no FFmpeg or model imports.

Family is an advisory relation, never proof of identical content. Sidecars do
not become sources. XMP fields are audit metadata, not new manual authority.
"""
from dataclasses import dataclass,asdict
import hashlib,json,shutil,subprocess
from pathlib import Path
from .v2_producer import fingerprint
from .v2_product_media import load_module
from .v2_task_materializer import _read_finder_xattr

PREVIEW='scripts/02_step01_step02_pipeline/step02_2_image_preview_from_db_safe_v6_20260709_182200.py'
SCANNER='scripts/02_step01_step02_pipeline/step01_source_scan_lineage_dedup_db_safe_v7_20260709_175400.py'

@dataclass(frozen=True)
class ImagePolicy:
    contract:str='image-a9t-preview-v1'
    long_edge:int=1280
    explicit_timestamps_ms:None=None
    @property
    def fingerprint(self):return fingerprint(asdict(self))
    def revision(self,content):return fingerprint((content,self.fingerprint))
    def timestamp(self,slot):
        if slot!=0:raise ValueError('IMAGE_HAS_NO_VIDEO_SAMPLING')
        return -1

def build_plan(inventory):
    """Metadata-only full inventory planning; DB snapshots remain source/group local."""
    module=load_module(PREVIEW);scanner=load_module(SCANNER)
    rows=[];tags=[]
    for file in inventory['files']:
        if file['media_kind']!='image':continue
        values,status=_read_finder_xattr(Path(file['path']))
        row={**file,'source_content_id':file['source_id'],'source_file_id':file['source_id'],
             'source_path':file['path'],'source_relative_path':file['relative_path'],
             'media_type':'image','media_kind':'image','next_action':'process',
             'source_size':file['bytes'],'source_mtime_ns':file['mtime_ns'],'finder_tag_status':status}
        for raw in values:
            name,color=scanner.parse_macos_tag(raw)
            tags.append({'source_content_id':file['source_id'],'tag_id':fingerprint((file['source_id'],raw)),
                         'tag_raw':raw,'tag_name':name,'tag_color':color})
        rows.append(row)
    from .v2_a9t_plan import plan_images
    plan=plan_images(module,rows,tags)
    # Frozen V25 already treats matching XMP presence as an image authority.
    # Do not invent a rating threshold or parse Adobe edit instructions.
    plan['sidecar_assets']=[{**r,'source_content_id':r['source_id'],'media_type':'metadata_sidecar',
        'relative_path':r['relative_path'],'source_relative_path':r['relative_path']} for r in inventory['files'] if r['media_kind']=='sidecar']
    plan['frontend_code_sha']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    plan['fingerprint']=fingerprint({k:v for k,v in plan.items() if k!='fingerprint'})
    return plan

def decode_preview(job,root):
    module=load_module(PREVIEW);job=dict(job)
    target=Path(root)/'previews'/(job['parent_source_content_id']+'.jpg')
    job['output_path']=str(target)
    result=module.run_sips_job(job) if job['backend']=='sips_direct_jpg' else module.run_ql_job(job,Path(root)/'quicklook')
    if result['status']!='success':raise ValueError('IMAGE_PREVIEW_FAILED:'+json.dumps(result,ensure_ascii=False))
    # The original is immutable. Normalize residual orientation on the owned
    # derivative, exactly once; never apply RAW orientation a second time to a
    # QuickLook preview that already carries orientation=1.
    from PIL import Image,ImageOps
    with Image.open(target) as image:
        orientation=image.getexif().get(274,1)
        if orientation not in (None,1):
            logical=ImageOps.exif_transpose(image).convert('RGB')
            temporary=target.with_suffix('.oriented.jpg')
            logical.save(temporary,quality=95,exif=logical.getexif(),**({'icc_profile':image.info['icc_profile']} if image.info.get('icc_profile') else {}))
            temporary.replace(target)
    result.update(output_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                  orientation_before_normalization=orientation,orientation_normalized=True)
    return result

def sidecar_metadata(path):
    """Bounded, non-authoritative XMP audit; no XML entity or DTD expansion."""
    import xml.etree.ElementTree as ET
    p=Path(path)
    if p.stat().st_size>1024*1024:raise ValueError('XMP_TOO_LARGE')
    data=p.read_bytes()
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():raise ValueError('XMP_DTD_NOT_ALLOWED')
    root=ET.fromstring(data);values={}
    allowed={'Rating','Label','Orientation','DateTimeOriginal','CreateDate','ModifyDate','CreatorTool','Copyright'}
    for element in root.iter():
        for key,value in element.attrib.items():
            short=key.split('}')[-1]
            if short in allowed:values[short]=value
    return {'sha256':hashlib.sha256(data).hexdigest(),'fields':values,'authority':'AUDIT_ONLY_NOT_SELECTION_AUTHORITY'}

def metadata(paths,required=False):
    """Existing ExifTool metadata fields, no pixel decode. Missing tool is explicit."""
    tool=shutil.which('exiftool') or next((str(p) for p in (Path('/opt/homebrew/bin/exiftool'),Path('/usr/local/bin/exiftool')) if p.is_file()),None)
    if not tool:
        if required:raise ValueError('METADATA_EXIFTOOL_REQUIRED_BEFORE_MODELS')
        return {str(p):{'status':'UNKNOWN_EXIFTOOL_UNAVAILABLE'} for p in paths}
    fields=['DateTimeOriginal','CreateDate','Make','Model','LensModel','ImageWidth','ImageHeight',
            'Orientation','GPSLatitude','GPSLongitude','Rating','Copyright','Software','ModifyDate']
    output={}
    for offset in range(0,len(paths),100):
        proc=subprocess.run([tool,'-json','-n','-fast2',*['-'+x for x in fields],
                             *map(str,paths[offset:offset+100])],capture_output=True,text=True,timeout=120)
        if proc.returncode not in (0,1):raise ValueError('METADATA_PROBE_FAILED:'+proc.stderr[:300])
        for row in json.loads(proc.stdout):
            path=row.pop('SourceFile')
            output[path]={'status':'ERROR' if row.get('Error') else 'READ_WITH_WARNING' if row.get('Warning') else 'READ',**row,
                '_extraction':{'tool':tool,'exit_code':proc.returncode,'stderr':proc.stderr[:2000]},
                '_unknown_fields':[field for field in fields if field not in row]}
    for path in paths:output.setdefault(str(path),{'status':'ERROR','Error':'EXIFTOOL_RETURNED_NO_RECORD'})
    return output

def verify_resume_authority(plan):
    if plan.get('frontend_code_sha')!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('IMAGE_FRONTEND_CHANGED_REQUIRES_EXPLICIT_REVISION')
    module=load_module(PREVIEW)
    if plan['algorithm_sha256']!=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest():
        raise ValueError('A9T_PREVIEW_ALGORITHM_CHANGED_ON_RESUME')
    expected={}
    for tag in plan['finder_tags']:expected.setdefault(tag['source_content_id'],[]).append(tag['tag_raw'])
    for row in plan['rows']:
        raw,status=_read_finder_xattr(Path(row['source_path']))
        if sorted(raw)!=sorted(expected.get(row['source_content_id'],[])):
            raise ValueError('FINDER_AUTHORITY_CHANGED_ON_RESUME:'+row['source_content_id'])
