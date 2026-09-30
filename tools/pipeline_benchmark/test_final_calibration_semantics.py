"""Regression tests for timing, resident semantics and formal output gates."""
import json
import tempfile
import unittest
from pathlib import Path
import run_full_pipeline_calibration as campaign


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


if __name__=='__main__': unittest.main()
