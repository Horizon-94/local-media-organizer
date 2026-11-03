"""CPU ViT-B-32 query text tower using the unchanged local CLIP parameters."""
from . import query_trace as trace

def create(model_path,model_name,device):
    if model_name!='ViT-B-32' or device!='cpu':raise ValueError('query_text_tower_configuration_unsupported')
    with trace.span('openclip_library_import'):
        import torch,open_clip
        from open_clip.model import _build_text_tower
        from safetensors import safe_open
    with trace.span('openclip_construct'):
        cfg=open_clip.get_model_config(model_name)
        model=_build_text_tower(cfg['embed_dim'],cfg['text_cfg'])
    with trace.span('openclip_weights_read'):
        with safe_open(model_path,framework='pt',device='cpu') as f:
            state={key:f.get_tensor(key) for key in model.state_dict()}
    with trace.span('openclip_weights_apply'):
        model.load_state_dict(state,strict=True)
    del state
    model.eval();tokenizer=open_clip.get_tokenizer(model_name)
    def encode(payload):
        with torch.inference_mode():
            vector=model(tokenizer([payload['text']])).float()
            vector=vector/vector.norm(dim=-1,keepdim=True)
        return {'vector':vector[0].cpu().tolist()}
    return encode
