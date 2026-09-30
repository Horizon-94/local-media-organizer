"""One percentile definition for all reports: empirical nearest rank."""
import math


def p95(values):
    ordered=sorted(values)
    return ordered[max(0,math.ceil(.95*len(ordered))-1)] if ordered else None


def post_exit_samples(rows, exit_epoch):
    """Read synchronized samples relative to the LAST formal worker exit.

    The shared coordinator/monitoring children may still exist. Report that
    tree honestly rather than claiming its remaining RSS is model residency.
    """
    result={}
    for offset in (10,30):
        eligible=[r for r in rows if r.get('timestamp_epoch')]
        row=min(eligible,key=lambda r:abs(float(r['timestamp_epoch'])-exit_epoch-offset)) if eligible else None
        if row is None or abs(float(row['timestamp_epoch'])-exit_epoch-offset)>2.5:
            result[f'post_exit_{offset}s']=None
            continue
        def number(key): return float(row[key]) if row.get(key) not in ('',None) else None
        total,available=number('physical_memory_bytes'),number('memory_available_approx_bytes')
        result[f'post_exit_{offset}s']={
            'timestamp_epoch':number('timestamp_epoch'),
            'observed_seconds_after_last_formal_exit':number('timestamp_epoch')-exit_epoch,
            'process_tree_rss_bytes':number('process_rss_kib')*1024 if number('process_rss_kib') is not None else None,
            'system_memory_used_bytes':total-available if total is not None and available is not None else None,
            'compressed_memory_bytes':number('memory_compressed_bytes'),
            'swap_used_bytes':number('swap_used_bytes'),
            'memory_pressure_free_percent':number('memory_pressure_free_percent'),
            'scope':'aggregate coordinator tree; formal models have exited, monitoring overhead may remain'}
    return result
