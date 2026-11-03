"""Library-scoped federation. Never merge person identities across databases."""
import hashlib
import sqlite3


def search_libraries(libraries, execute, *, offset=0, limit=30):
    offset, limit = max(0, offset), max(1, min(200, limit))
    wanted = offset + limit
    items, errors, total = [], [], 0
    coverage = {}
    coverage_complete = True
    coverage_fields = {'eligible_visual_unit_count', 'scanned_visual_vector_count', 'scanned_text_vector_count'}
    for library in libraries:
        prefix = hashlib.sha256(library['database'].encode()).hexdigest()[:24]
        local, cursor, visited = [], 0, set()
        try:
            while len(local) < wanted:
                if cursor in visited:
                    raise RuntimeError('library_pagination_not_advancing')
                visited.add(cursor)
                report = execute(library, cursor)
                if report.get('status', 'PASS') != 'PASS':
                    raise RuntimeError(report.get('error_reason') or report.get('error') or 'search_failed')
                batch = report.get('result_items') or []
                local.extend(batch)
                if cursor == 0:
                    library_total = int(report.get('result_total_count', len(batch)))
                    library_coverage = report.get('coverage') or {}
                cursor = report.get('next_result_offset')
                if cursor is None:
                    break
            total += library_total
            if not all(isinstance(library_coverage.get(key), int) for key in coverage_fields):
                coverage_complete = False
            for name, value in library_coverage.items():
                if isinstance(value, (int, float)):
                    coverage[name] = coverage.get(name, 0) + value
            for rank, original in enumerate(local[:wanted], 1):
                row = dict(original)
                local_id = str(row.get('result_id') or row.get('visual_unit_id') or rank)
                row.update(result_id=prefix+':'+local_id,
                           library_local_result_id=local_id,
                           library_task_path=library['task_path'],
                           library_name=library['task_name'],
                           library_database=library['database'],
                           federation_rank=rank)
                items.append(row)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            coverage_complete = False
            errors.append({'library': library['task_name'], 'reason': str(exc)})
    # Scores from different libraries are not calibrated. Stable reciprocal
    # rank order exposes each library's best match without comparing raw scores.
    items.sort(key=lambda r: (r['federation_rank'], r['library_database'], r['result_id']))
    selected = items[offset:offset+limit]
    return dict(status='PASS' if len(errors) < len(libraries) else 'FAIL',
                result_items=selected, result_count=len(selected),
                result_total_count=total, result_offset=offset, result_limit=limit,
                next_result_offset=offset+limit if offset+limit < total else None,
                coverage=coverage if coverage_complete else None, library_errors=errors,
                partial_results=bool(errors), search_results_read_only=True,
                error='所选素材库均无法搜索' if len(errors) == len(libraries) else None,
                cross_library_person_merge=False)
