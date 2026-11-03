"""Bounded dispatch to the existing final projection implementation by kind."""
from apps.media_archive_image_video_ui.video_peer_catalog import dirty_heads
from apps.media_archive_image_video_ui.video_detached_coordination import pipeline_operation

def refresh(runtime,limit=4):
    with runtime.adapters.store.connect() as c:
        heads=dirty_heads(c,limit,getattr(runtime,'peer_refresh_cursor',''))
        if not heads:heads=dirty_heads(c,limit)
    if heads:runtime.peer_refresh_cursor=heads[-1][0]
    for source,revision,snapshot in heads:
        adapter=runtime.for_source(source)
        def calculate(p):
            with p.store.connect() as c:old=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
            if not p.final_projection.advance(source,revision,snapshot):return False
            with p.store.connect() as c:
                new=c.execute('SELECT execution_key FROM v2_final_projection_heads WHERE source_id=? AND source_revision=?',(source,revision)).fetchone()
                if old and new and old[0]!=new[0]:c.execute("DELETE FROM video_stream_source_states_v1 WHERE source_id=? AND revision=? AND state IN ('SOURCE_COMPLETE','VISUAL_COMPLETE')",(source,revision))
            return True
        pipeline_operation(adapter,source,revision,'final_projection',calculate)
    return len(heads)
