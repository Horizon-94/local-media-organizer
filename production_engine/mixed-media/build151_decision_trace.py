"""Read-only decision telemetry. No admission mutations or source selection."""
import json,time

def changed(runtime,event,key,values,decision=False):
    cache=getattr(runtime,'_build151_trace_cache',None)
    if cache is None:cache={};runtime._build151_trace_cache=cache
    signature=json.dumps(values,sort_keys=True,default=str)
    if cache.get((event,key))==signature and not decision:return
    cache[event,key]=signature
    try:
        with (runtime.root/'build151-decisions.jsonl').open('a') as f:
            f.write(json.dumps(dict(event=event,timestamp=time.time(),**values),ensure_ascii=False,default=str)+'\n')
    except OSError:pass
