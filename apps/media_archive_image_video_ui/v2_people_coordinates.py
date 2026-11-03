"""Product coordinate adapter: CV2/YOLO use the EXIF-oriented display raster."""
import json
from PIL import Image
from .v2_people import PersonIdentity
from .v2_producer import fingerprint
from .v2_runtime import ContractError


def display_geometry(artifact):
    with Image.open(artifact['path']) as image:
        stored=image.size;orientation=image.getexif().get(274,1)
    if stored!=(artifact['width'],artifact['height']):
        raise ContractError('person_artifact_raster_dimensions_changed')
    width,height=stored[::-1] if orientation in (5,6,7,8) else stored
    return width,height,orientation


class DisplayAwarePersonIdentity(PersonIdentity):
    def ingest_ready(self,source,revision):
        # Preserve the frozen observation/association contract and original
        # evidence. Only normalization dimensions follow the model's raster.
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            artifacts=list(c.execute('SELECT * FROM v2_artifacts WHERE source_id=? AND revision=? ORDER BY slot',(source,revision)))
            for artifact in artifacts:
                y=c.execute("SELECT * FROM v2_jobs WHERE stage='yoloe' AND item_id=? ORDER BY revision DESC LIMIT 1",(artifact['artifact_id'],)).fetchone()
                face=c.execute("SELECT * FROM v2_jobs WHERE stage='insightface' AND item_id=? ORDER BY revision DESC LIMIT 1",(artifact['artifact_id'],)).fetchone()
                if not y or y['status']!='DONE' or y['input_sha']!=artifact['sha']:continue
                available=face and face['status']=='DONE' and face['input_sha']==artifact['sha']
                faces=json.loads(face['result_json'])['faces'] if available else []
                detections=[(i,d) for i,d in enumerate(json.loads(y['result_json'])['detections']) if d['label'].lower()=='person']
                width,height,orientation=display_geometry(artifact) if detections or faces else (artifact['width'],artifact['height'],1)
                boxes=[]
                for i,d in detections:
                    bbox=d.get('bbox',d.get('bbox_xyxy'))
                    if not bbox or len(bbox)!=4:raise ContractError('yolo_person_bbox_missing')
                    normalized=[v/(width if j%2==0 else height) for j,v in enumerate(bbox)]
                    boxes.append((i,d,normalized))
                associated={i:[] for i,_,_ in boxes}
                for i,f in enumerate(faces):
                    x=(f['bbox'][0]+f['bbox'][2])/2/width;ypos=(f['bbox'][1]+f['bbox'][3])/2/height
                    possible=[idx for idx,_,b in boxes if b[0]<=x<=b[2] and b[1]<=ypos<=b[3]]
                    if len(possible)!=1:continue
                    associated[possible[0]].append({**f,'vector':f.get('embedding'),'quality':f['quality_score'],
                        'face_ref':face['execution_key']+':'+str(i),'model_contract':fingerprint(json.loads(face['provenance']))})
                c.execute('DELETE FROM v2_observation_heads WHERE artifact_id=?',(artifact['artifact_id'],))
                for i,d,b in boxes:
                    key=fingerprint((y['execution_key'],face['execution_key'] if available else 'FACE_MISSING',i))
                    provenance={'yolo_execution_key':y['execution_key'],'input_sha':artifact['sha'],
                        'face_execution_key':face['execution_key'] if available else None,'face_status':'READY' if available else 'MISSING',
                        'yolo_contract':json.loads(y['provenance']),'association':'FACE_CENTER_CONTAINMENT_PROXY',
                        'logical_mapping_ambiguous':sum(1 for _,_,box in boxes if box==b)>1}
                    if orientation in (5,6,7,8):
                        authority={'contract':'person_exif_display_dimensions_v1','orientation':orientation,
                            'stored_width':artifact['width'],'stored_height':artifact['height'],
                            'display_width':width,'display_height':height,'artifact_sha':artifact['sha']}
                        provenance['coordinate_authority']=authority;key=fingerprint((key,authority))
                    self.observation(c,source,revision,artifact['artifact_id'],artifact['time_ms'],b,d['confidence'],associated[i],provenance,y['revision'],key,detection_slot=i)
