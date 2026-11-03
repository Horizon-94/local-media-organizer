"""Reuse only a proven-empty delivery query, fenced by durable mutation generations.

No event cursor/watermark is used. Late timestamps, old ROWIDs, inbox repair and
ownership updates all invalidate the proof in their own SQLite transaction.
"""


def install(c):
    c.execute('''CREATE TABLE IF NOT EXISTS v2_delivery_generations(
        kind TEXT,key TEXT,generation INTEGER NOT NULL,PRIMARY KEY(kind,key))''')
    c.execute("INSERT OR IGNORE INTO v2_delivery_generations VALUES('outbox','*',1)")
    for action in ('INSERT','UPDATE','DELETE'):
        c.execute('''CREATE TRIGGER IF NOT EXISTS v2_delivery_outbox_'''+action.lower()+'''
            AFTER '''+action+''' ON v2_outbox BEGIN
            UPDATE v2_delivery_generations SET generation=generation+1 WHERE kind='outbox' AND key='*'; END''')
        aliases=('old','new') if action=='UPDATE' else (('old',) if action=='DELETE' else ('new',))
        statements=''.join("INSERT INTO v2_delivery_generations VALUES('inbox',"+alias+".consumer,1) ON CONFLICT(kind,key) DO UPDATE SET generation=generation+1;" for alias in aliases)
        c.execute('CREATE TRIGGER IF NOT EXISTS v2_delivery_inbox_'+action.lower()+' AFTER '+action+' ON v2_inbox BEGIN '+statements+' END')


def discover(store,c,consumer,subscription,sql,args,*,query=None):
    # The caller has already asserted its exact current owner and opens the
    # ordinary owned connection. This read and the event query share a snapshot.
    if not c.in_transaction:
        # Read transaction only: the guarded BEGIN would acquire a writer lock.
        # Ownership was asserted by the caller/owned connection already.
        import sqlite3
        sqlite3.Connection.execute(getattr(c,'connection',c),'BEGIN')
    generations=c.execute('''SELECT
        (SELECT generation FROM v2_delivery_generations WHERE kind='outbox' AND key='*'),
        COALESCE((SELECT generation FROM v2_delivery_generations WHERE kind='inbox' AND key=?),0)''',(consumer,)).fetchone()
    key=(store.scheduler_run_id,store.execution_epoch,consumer,subscription)
    proof=tuple(generations)
    cache=getattr(store,'_empty_delivery_proofs',{})
    if proof[0] is not None and cache.get(key)==proof:
        perf=getattr(c,'perf',None)
        if perf:perf.count('EMPTY_DELIVERY_PROOF_SKIP')
        return []
    perf=getattr(c,'perf',None)
    if perf:perf.count('OUTBOX_DISCOVERY_SCAN')
    rows=list(query() if query is not None else c.execute(sql,args))
    if not rows and proof[0] is not None:cache[key]=proof
    else:cache.pop(key,None)
    while len(cache)>64:cache.pop(next(iter(cache)))
    store._empty_delivery_proofs=cache
    return rows


def subscribed_rows(c,run,epoch,consumer,types,limit):
    """Merge bounded per-type heads in the same discovery read snapshot.

    No watermark: old timestamps and repaired inbox records remain eligible.
    Each type needs at most the global limit to determine the global head.
    """
    rows=[]
    for kind in types:
        rows.extend(c.execute('''SELECT o.payload,o.committed_at,o.event_id
            FROM v2_outbox o INDEXED BY v2_outbox_owner_type_order
            WHERE o.scheduler_run_id=? AND o.execution_epoch=? AND o.event_type=?
            AND NOT EXISTS(SELECT 1 FROM v2_inbox i
                WHERE i.consumer=? AND i.event_id=o.event_id)
            ORDER BY o.committed_at,o.event_id LIMIT ?''',
            (run,epoch,kind,consumer,limit)))
    rows.sort(key=lambda row:(row[1],row[2]))
    return rows[:limit] if limit>=0 else rows
