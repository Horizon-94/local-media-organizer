"""Regression tests for timing, resident semantics and formal output gates."""
import json
import tempfile
import unittest
import os
import sys
from pathlib import Path
import run_full_pipeline_calibration as campaign
import trace_formal_stage as trace
from audit_calibration_validity import concurrency_evidence


class Semantics(unittest.TestCase):
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
