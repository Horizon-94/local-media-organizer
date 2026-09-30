"""Regression tests for timing, resident semantics and formal output gates."""
import json
import tempfile
import unittest
import os
import sys
from unittest.mock import patch
from pathlib import Path
import run_full_pipeline_calibration as campaign
import trace_formal_stage as trace
import host_resource_monitor as host_monitor
from audit_calibration_validity import concurrency_evidence, inspect_result, input_fingerprint
from measurement_stats import p95, post_exit_samples


def encode(): return 42
def spawned_encode_probe(): encode()


class Semantics(unittest.TestCase):
    def test_iostat_transfer_fields_are_not_read_write_bytes(self):
        with patch.object(host_monitor,'run',return_value=(0,'disk0\nKB/t xfrs MB\n12.1 30 40.2\n','')):
            sample=host_monitor.disk_snapshot()
        self.assertIsNone(sample['disk_read_cumulative'])
        self.assertIsNone(sample['disk_write_cumulative'])
        self.assertEqual(sample['disk_combined_cumulative_iostat_mb'],40.2)

    def test_composite_recovery_uses_last_model_exit_not_coordinator_exit(self):
        rows=[{'timestamp_epoch':str(t),'physical_memory_bytes':'1000','memory_available_approx_bytes':str(f),'process_rss_kib':'1'} for t,f in ((60,200),(80,300),(90,400),(110,500))]
        observations=post_exit_samples(rows,50)
        self.assertEqual(observations['post_exit_10s']['timestamp_epoch'],60)
        self.assertEqual(observations['post_exit_30s']['timestamp_epoch'],80)
        self.assertEqual(observations['post_exit_10s']['system_memory_used_bytes'],800)
        self.assertIsNone(post_exit_samples([],50)['post_exit_10s'])

    def test_whisper_composite_waits_for_backend_not_process_start(self):
        with tempfile.TemporaryDirectory() as td:
            seen=[]
            def fake(row,root):
                seen.append(row['kind'])
                return {'status':'PASS'}
            with patch.object(campaign,'run_formal_adapter',side_effect=fake):
                result=campaign.run_composite({'id':'whisper+openclip'},Path(td))
            self.assertEqual(result['reason_code'],'whisper_backend_start_not_observed')
            self.assertEqual(seen,['whisper'])

    def test_whisper_backend_marker_allows_visual_launch(self):
        with tempfile.TemporaryDirectory() as td:
            def fake(row,root):
                if row['kind']=='whisper':
                    marker=root/'stage/items/item/benchmark-whisper-events.jsonl'
                    marker.parent.mkdir(parents=True); marker.write_text('{"event":"FORMAL_WHISPER_BACKEND_START"}\n')
                else:
                    self.assertTrue(list((Path(td)/'whisper').glob('**/benchmark-whisper-events.jsonl')))
                return {'status':'PASS'}
            with patch.object(campaign,'run_formal_adapter',side_effect=fake):
                result=campaign.run_composite({'id':'whisper+openclip'},Path(td))
            self.assertEqual(result['status'],'PASS')
            self.assertEqual(result['launch_policy'],'whisper_backend_start_then_visual_models')

    def test_ffmpeg_fingerprint_checks_content_not_only_count(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); p=root/'stage/manifests/source_video_identity_step01_queue.csv'
            p.parent.mkdir(parents=True)
            header='source_video_fingerprint_sha256,sampling_contract_id\n'
            p.write_text(header+'sha-a,contract-3s\nsha-b,contract-3s\n')
            before=input_fingerprint('ffmpeg-stage03-x1',{'output':td})
            p.write_text(header+'sha-b,contract-3s\nsha-a,contract-3s\n')
            self.assertEqual(before,input_fingerprint('ffmpeg-stage03-x2',{'output':td}))
            p.write_text(header+'sha-a,contract-3s\nsha-c,contract-3s\n')
            self.assertNotEqual(before,input_fingerprint('ffmpeg-stage03-x2',{'output':td}))

    def test_composite_cannot_hide_invalid_constituent(self):
        with tempfile.TemporaryDirectory() as td:
            samples=Path(td)/'samples.csv'
            samples.write_text('phase,timestamp_epoch,process_rss_kib,process_cpu_percent\nactive,1,100,10\n')
            result={'status':'PASS','output':td,'workers':1,'metrics':{'artifacts':{'samples_csv':str(samples)}},'constituents':[{'status':'HARNESS_FAIL'}],'constituent_intervals':[[0,2]],'workload_elapsed_seconds':2,'all_constituents_overlap_seconds':2}
            row=inspect_result('yoloe+openclip',result)
            self.assertEqual(row['validity'],'INVALID')
            self.assertEqual(row['resource_verdict'],'HARNESS_INVALID')

    def test_spawn_observer_keeps_real_child_pid(self):
        with tempfile.TemporaryDirectory() as td:
            keys=('BENCHMARK_TRACE_STAGE','BENCHMARK_TRACE_DIR')
            previous={k:os.environ.get(k) for k in keys}
            os.environ.update(BENCHMARK_TRACE_STAGE=Path(__file__).name,BENCHMARK_TRACE_DIR=td)
            try:
                process=trace.TracedSpawnProcess(target=spawned_encode_probe)
                process.start(); process.join(10)
                self.assertEqual(process.exitcode,0)
                row=json.loads(next(Path(td).glob('worker-*.jsonl')).read_text())
                self.assertEqual(row['pid'],process.pid)
                self.assertNotEqual(row['pid'],os.getpid())
            finally:
                for key,value in previous.items():
                    if value is None: os.environ.pop(key,None)
                    else: os.environ[key]=value
    def test_p95_short_sample_is_not_zero_when_peak_is_positive(self):
        self.assertEqual(p95([0,0,100]),100)
        self.assertIsNone(p95([]))
    def test_resident_is_not_cold_alias(self):
        row = next(r for r in campaign.campaign_plan() if r['id']=='whisper-resident-x1')
        self.assertEqual(row['mode'], 'resident')
        self.assertEqual(campaign.run_formal_adapter(row,Path('/tmp/unused-resident'))['status'], 'NOT_SUPPORTED_YET')

    def test_empty_success_is_rejected(self):
        for kind, report in [('embedding','stage/reports/stop03_5d_full_summary.json'),
                             ('whisper','stage/audio_enrichment_summary.json'),
                             ('person-reid','stage/run_summary.json'),
                             ('qwen','qwen-stage/reports/final_report.json')]:
            with tempfile.TemporaryDirectory() as td:
                path=Path(td)/report; path.parent.mkdir(parents=True)
                path.write_text(json.dumps({'status':'PASS','run_status':'success','counts':{}}))
                self.assertFalse(campaign.formal_stage_result(kind,Path(td))[0])

    def test_unknown_parser_fails_closed(self):
        self.assertEqual(campaign.formal_stage_result('missing',Path('/tmp')), (False,'formal_success_parser_missing'))

    def test_resource_verdict_does_not_hide_swap_or_harness_failure(self):
        self.assertEqual(campaign.classify_observed_resources({'status':'PASS','metrics':{'swap_delta_bytes':1}}),'BORDERLINE')
        self.assertEqual(campaign.classify_observed_resources({'status':'HARNESS_FAIL'}),'HARNESS_INVALID')
        self.assertEqual(campaign.classify_observed_resources({'status':'SKIPPED_DUE_TO_UNSAFE_PARENT'}),'SKIPPED')

    def test_parallel_audio_jobs_are_not_parallel_whisper_inference(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'stage').mkdir()
            (root/'stage/audio_enrichment_summary.json').write_text(json.dumps({'status':'SUCCESS','source_video_count':3,'transcript_segment_count':10,'vector_count':10,'counts':{'success':1,'no_speech':2}}))
            samples=root/'samples.csv'
            samples.write_text('phase,timestamp_epoch,process_rss_kib,process_cpu_percent,memory_pressure_free_percent\nactive,1,100,10,80\n')
            row=inspect_result('whisper-x2',{'status':'PASS','workers':2,'output':td,'metrics':{'artifacts':{'samples_csv':str(samples)},'events':[{'event':'BENCHMARK_START','timestamp_epoch':0},{'event':'PROCESS_EXIT','timestamp_epoch':2}]}})
            self.assertEqual(row['validity'],'INVALID')
            self.assertEqual(row['speech_inference_inputs'],1)

    def test_task_trace_and_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            previous={k:os.environ.get(k) for k in ('BENCHMARK_TRACE_STAGE','BENCHMARK_TRACE_DIR')}
            root=Path(td)/'stage/benchmark-task-timing'
            os.environ.update(BENCHMARK_TRACE_STAGE='fixture.py', BENCHMARK_TRACE_DIR=str(root))
            try:
                ns={}
                exec(compile('def infer_one(): return 42','fixture.py','exec'),ns)
                sys.setprofile(trace.profile)
                self.assertEqual(ns['infer_one'](),42)
                sys.setprofile(None)
                record=json.loads(next(root.glob('*.jsonl')).read_text())
                self.assertEqual(record['pid'],os.getpid())
                self.assertGreater(record['finished_epoch'],record['started_epoch'])
                (root/'worker-synthetic.jsonl').write_text(json.dumps({'started_epoch':record['started_epoch'], 'finished_epoch':record['finished_epoch']})+'\n')
                self.assertEqual(concurrency_evidence('openclip',{'output':td}),2)
            finally:
                sys.setprofile(None)
                for key,value in previous.items():
                    if value is None: os.environ.pop(key,None)
                    else: os.environ[key]=value


if __name__=='__main__': unittest.main()
