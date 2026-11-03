"""Product media admission through the frozen Stage03 and A9T functions.

No worker scheduling or selection policy lives here. Callers supply the complete
Step01 source snapshot (including sidecars), not preselected candidate quotas.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import uuid

from .v2_producer import fingerprint
from .v2_model_workers import sha_file
from .v2_runtime import ContractError
from .v2_selection import ROOT
from .v2_image_bridge import publish_preview


def load_module(relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location('product_media_' + uuid.uuid4().hex, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProductMediaProducer:
    def __init__(self, driver, workspace):
        self.driver = driver
        self.root = Path(workspace) / 'v2-media' / fingerprint(dict(driver.owner))

    def _owned(self):
        self.driver.store.assert_current_owner()
        # Includes the contract check at the authoritative persistence boundary.
        with self.driver.store.connect() as c:
            c.execute('SELECT 1')

    def video(self, source, *, interval_seconds=3, offset_ms=2000):
        self._owned()
        if getattr(self.driver,'raw_visual_skip_enabled',False):
            from .v2_raw_audio_lane import produce_raw_video
            skipped=produce_raw_video(self.driver,source)
            if skipped is not None:return skipped
        source = deepcopy(source)
        if source.get('media_kind', source.get('media_type')) != 'video':
            raise ContractError('video_source_metadata_required')
        if not source.get('source_content_id') or not source.get('source_relative_path'):
            raise ContractError('source_identity_and_relative_path_required')
        adapter = load_module('scripts/04_media_archive_app/step02_video_frame_generic_interval_v1.py')
        frozen = adapter.load_frozen_extractor(ROOT)
        contract = adapter.configure_sampling_contract(frozen, interval_seconds, offset_ms)
        frozen.OUT = self.root / 'video'
        frozen.IDENTITY_DIR = frozen.OUT / 'identity'
        frozen.IDENTITY_DIR.mkdir(parents=True, exist_ok=True)
        self.driver.install_video_adapter(frozen)
        base = frozen.make_source_base_from_queue_row(source)
        base.update(source_metadata=source, media_type='video', source_relative_path=source['source_relative_path'],
                    product_contract_version=self.driver.owner['pipeline_contract_version'])
        lease = None
        if self.driver.admission_scheduler is not None:
            lease = self.driver.admission_scheduler.acquire(
                source['source_content_id'], 'video_source_ingest',
                source_path=source['source_path'], requires_original=True,
                scheduler_run_id=self.driver.owner['scheduler_run_id'],
                execution_epoch=self.driver.owner['execution_epoch'])
        try:
            result = frozen.process_one(1, 1, Path(source['source_path']), base,
                frozen.OUT / fingerprint(source['source_content_id']), 'process_new')
            self._owned()
            if lease is not None:
                derivative = result[1][0].get('path') if result[1] else frozen.OUT
                self.driver.admission_scheduler.publish_derivative(lease, derivative)
        except Exception:
            if lease is not None:
                self.driver.admission_scheduler.release(lease, reason='source_ingest_failed')
            raise
        return {'source_id': source['source_content_id'], 'source_revision': result[0]['v2_source_revision'],
                'report': result[0], 'frames': result[1], 'sampling_contract': contract}

    def images(self, queue_rows, *, source_assets, finder_tags, canonical_sources=None, canonical_revision=None, image_plan=None):
        """Preprocess a dependency-closed Step01 batch, then bind B2 inputs.

        canonical_sources is an explicitly supplied existing source-identity map;
        its revision is mandatory. No visual similarity or candidate policy is
        inferred here. Every canonical target must have a published preview.
        """
        self._owned()
        rows, assets, tags = deepcopy(queue_rows), deepcopy(source_assets), deepcopy(finder_tags)
        canonical_sources = deepcopy(canonical_sources or {})
        if canonical_sources and not canonical_revision:
            raise ContractError('canonical_context_lineage_missing')
        if any(canonical_sources.get(target, target) != target for target in canonical_sources.values()):
            raise ContractError('canonical_target_must_be_an_owner')
        known = {r['source_content_id'] for r in assets}
        if any(not r.get('source_content_id') or not r.get('source_relative_path') or r['source_content_id'] not in known for r in rows):
            raise ContractError('complete_source_snapshot_required')
        module = load_module('scripts/02_step01_step02_pipeline/step02_2_image_preview_from_db_safe_v6_20260709_182200.py')
        for row in rows:
            row['finder_tags'] = [r.get('tag_name') or r.get('tag_raw') for r in tags if r['source_content_id'] == row['source_content_id']]
        items = module.build_items(rows)
        if image_plan is None:
            detected = module.detect_timelapse(items, module.exiftool_available())
        else:
            from .v2_a9t_plan import detection_for
            if image_plan['algorithm_sha256'] != sha_file(module.__file__):
                raise ContractError('a9t_plan_algorithm_changed')
            detected = detection_for(image_plan,rows,tags)
        context = (rows, assets, tags, canonical_sources, canonical_revision, sha_file(module.__file__))
        context_key = fingerprint(context if image_plan is None else (*context,image_plan['fingerprint']))
        root = self.root / 'images' / context_key
        dirs = {'normal': root/'normal', 'keyframes': root/'keyframes'}
        decisions, jobs = module.plan_jobs(items, dirs, detected[3], detected[4], detected[5])
        by_source = {r['source_content_id']: r for r in rows}
        producer = self.driver.producer(artifact_root=self.root/'artifacts')
        dependencies, previews, visual_by_source = {}, [], {}
        for job in jobs:
            self._owned()
            sid = job['parent_source_content_id']
            row = by_source[sid]
            lease = None
            if self.driver.admission_scheduler is not None:
                lease = self.driver.admission_scheduler.acquire(
                    sid, 'image_source_ingest', source_path=row['source_path'],
                    requires_original=True,
                    scheduler_run_id=self.driver.owner['scheduler_run_id'],
                    execution_epoch=self.driver.owner['execution_epoch'])
            digest = sha_file(row['source_path'])
            metadata = {**row, 'media_type': 'image', 'relative_path': row['source_relative_path'],
                        'a9t_plan': job, 'a9t_contract': module.SCHEME, 'context_fingerprint': context_key}
            revision = producer.source_ready(sid, fingerprint((digest, context_key)), digest, row['source_path'], metadata, execution_attempt=row.get('_product_attempt_id'))
            with producer.source_lease(sid, revision):
                result = module.run_sips_job(job) if job['backend'] == 'sips_direct_jpg' else module.run_ql_job(job, root/'quicklook')
                if result['status'] != 'success':
                    producer.abort_decode_attempt(sid, revision, 'a9t_preview_failed')
                    raise ContractError('a9t_preview_failed:' + str(result.get('validation_reason')))
                if sha_file(row['source_path']) != digest:
                    producer.abort_decode_attempt(sid, revision, 'source_changed_during_preprocessing')
                    raise ContractError('source_changed_during_preprocessing')
                # Preserve the A9T manifest/DB identities while publishing the
                # audited V2 visual ID; never confuse preview ID with visual ID.
                result['a9t_visual_unit_id'] = 'vu_' + module.sha256_text(
                    f"visual_unit_v1|{result['preview_artifact_id']}|{result['output_sha256']}", 24)
                result['a9t_derived_id'] = 'der_' + module.sha256_text(
                    f"{module.DB_DERIVED_TYPE}|{result['preview_artifact_id']}|{result['output_sha256']}|{result['output_path']}", 24)
                mapping = publish_preview(producer, sid, revision, 0, {**result, 'visual_unit_id': result['a9t_visual_unit_id']})
                producer.finish(sid, revision, 0, success=True)
                if lease is not None:
                    self.driver.admission_scheduler.publish_derivative(lease, result['output_path'])
            if lease is not None and lease.token_id in {t.token_id for t in self.driver.admission_scheduler.active_tokens()}:
                self.driver.admission_scheduler.release(lease, reason='image_ingest_complete')
            dependencies[sid] = revision
            visual_by_source[sid] = mapping['visual_unit_id']
            previews.append({**result, **mapping, 'source_id': sid, 'source_revision': revision})
        if any(s not in visual_by_source or t not in visual_by_source for s,t in canonical_sources.items()):
            raise ContractError('canonical_or_sequence_dependency_missing')
        mappings, imported, timelapse = [], {}, []
        for preview in previews:
            sid, visual = preview['source_id'], preview['visual_unit_id']
            canonical = visual_by_source[canonical_sources.get(sid, sid)]
            mapping = {'visual_unit_id': visual, 'source_content_id': sid, 'canonical_visual_unit_id': canonical,
                       'visual_duplicate_group_id': fingerprint((canonical_revision, canonical)),
                       'identity_status': 'canonical' if visual == canonical else 'duplicate'}
            mappings.append(mapping)
            if visual != canonical:
                imported[visual] = {k: mapping[k] for k in ('canonical_visual_unit_id','visual_duplicate_group_id','identity_status')}
            if preview['preview_role'] == 'timelapse_keyframe':
                timelapse.append({'visual_unit_id': visual, 'sequence_id': preview['sequence_id'],
                    'representative_position': preview['representative_position'], 'source_relative_path': preview['source_relative_path'],
                    'visual_file': preview['output_path'], 'parent_source_content_id': sid})
        image_inputs = {'source_assets': assets, 'finder_tags': tags, 'visual_mappings': mappings, 'timelapse_rows': timelapse}
        from .v2_timelapse_selection_context import sequence_members
        partial_sequence=any(set(r['parent_source_content_id'] for r in sequence_members(image_plan,sid))-set(dependencies) for sid in dependencies)
        if dependencies and not partial_sequence:
            self.driver.pipeline.selection.bind_context(dependencies, image_inputs,
                canonical_mappings=imported, canonical_revision=canonical_revision if imported else None)
        elif partial_sequence:
            normal={sid:rev for sid,rev in dependencies.items() if not sequence_members(image_plan,sid)}
            if normal:
                local=[r for r in mappings if r['source_content_id'] in normal]
                ids={r['visual_unit_id'] for r in local}
                aliases={key:value for key,value in imported.items() if key in ids}
                self.driver.pipeline.selection.bind_context(normal,{**image_inputs,'visual_mappings':local,'timelapse_rows':[]},
                    canonical_mappings=aliases,canonical_revision=canonical_revision if aliases else None)
        # Durable preprocessing report, protected by the same transaction fence.
        report = {'dependencies': dependencies, 'previews': previews, 'decisions': decisions,
                  'image_inputs': image_inputs, 'timelapse_detection': detected[:3]}
        with self.driver.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS v2_product_media_reports(report_id TEXT PRIMARY KEY,scheduler_run_id TEXT,execution_epoch INTEGER,payload TEXT)')
            from .v2_shared_payload import dump
            c.execute('INSERT OR REPLACE INTO v2_product_media_reports VALUES(?,?,?,?)',
                (fingerprint((context_key, dict(self.driver.owner))), self.driver.owner['scheduler_run_id'], self.driver.owner['execution_epoch'], dump(c, report)))
        return report
