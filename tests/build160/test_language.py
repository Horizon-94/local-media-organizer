from pathlib import Path
import sys,copy,unittest,types,os
from unittest.mock import patch
sys.dont_write_bytecode=True
P=Path(__file__).resolve().parents[2];sys.path[:0]=[str(P),str(P/'production_engine/mixed-media')]
from apps.media_archive_image_video_ui.chinese_text import simplified_chinese,whisper_parameters,POLICY
from apps.media_archive_image_video_ui.v2_model_workers import create_handler
import asr_quality as quality
class Language(unittest.TestCase):
 def test_chinese(self):self.assertEqual(simplified_chinese('我們的小麥與對話'),'我们的小麦与对话')
 def test_mixed(self):self.assertEqual(simplified_chinese('小麥 OpenAI Whisper OK 01:23 123'),'小麦 OpenAI Whisper OK 01:23 123')
 def test_english_unchanged(self):self.assertEqual(simplified_chinese('Hello world!'),'Hello world!')
 def test_idempotent(self):self.assertEqual(simplified_chinese(simplified_chinese('繁體中文')),'繁体中文')
 def test_null_rejected(self):
  with self.assertRaises(ValueError):simplified_chinese('中文\x00尾部')
 def test_params(self):
  p=whisper_parameters({});self.assertEqual(p['language'],'zh');self.assertEqual(p['task'],'transcribe');self.assertIn('简体中文',p['initial_prompt'])
 def test_auto_defaults_chinese(self):self.assertEqual(whisper_parameters({'language':None})['language'],'zh')
 def test_explicit_english(self):self.assertEqual(whisper_parameters({'language':'en'})['language'],'en')
 def test_translation_rejected(self):
  with self.assertRaises(ValueError):whisper_parameters({'task':'translate'})
 def raw(self):return {'text':'我們的小麥 OpenAI','language':'zh','segments':[{'text':'我們的小麥 OpenAI','start':0.,'end':1.,'avg_logprob':-.2,'compression_ratio':1.,'no_speech_prob':.01}]}
 def test_worker_boundary(self):
  original=self.raw();snapshot=copy.deepcopy(original);seen={};m=types.ModuleType('mlx_whisper.transcribe')
  def infer(*args,**kwargs):seen.update(kwargs);return original
  m.transcribe=infer
  with patch.dict(sys.modules,{'mlx_whisper':types.ModuleType('mlx_whisper'),'mlx_whisper.transcribe':m}):
   handler=create_handler('whisper',{'model_path':'/fixture','parameters':{},'text_normalization':POLICY});result=handler({'path':'fixture.wav','start_ms':0,'end_ms':1000})
  self.assertEqual(seen['language'],'zh');self.assertEqual(seen['task'],'transcribe');self.assertEqual(result['text'],'我们的小麦 OpenAI');self.assertEqual(result['raw'],snapshot);self.assertEqual(original,snapshot)
  packet=quality.guarded_inference(lambda _:result,{'path':'fixture.wav','start_ms':0,'end_ms':1000},lambda _:self.fail('unexpected repair'))
  quality.validate_packet(packet);self.assertEqual(packet['usable_text'],'我们的小麦 OpenAI');self.assertEqual(packet['usable_timeline'][0]['source_end_ms'],1000);self.assertEqual(packet['asr_attempts'][0]['raw_result']['raw'],snapshot)
 def test_legacy_worker_contract_unchanged(self):
  original=self.raw();seen={};m=types.ModuleType('mlx_whisper.transcribe')
  def infer(*args,**kwargs):seen.update(kwargs);return original
  m.transcribe=infer
  with patch.dict(sys.modules,{'mlx_whisper':types.ModuleType('mlx_whisper'),'mlx_whisper.transcribe':m}):
   result=create_handler('whisper',{'model_path':'/fixture','parameters':{}})({'path':'fixture.wav','start_ms':0,'end_ms':1000})
  self.assertNotIn('language',seen);self.assertNotIn('initial_prompt',seen);self.assertEqual(result,{'text':original['text'],'language':'zh','raw':original})
 def test_unknown_policy_rejected(self):
  m=types.ModuleType('mlx_whisper.transcribe');m.transcribe=lambda *a,**k:None
  with patch.dict(sys.modules,{'mlx_whisper':types.ModuleType('mlx_whisper'),'mlx_whisper.transcribe':m}):
   with self.assertRaises(Exception):create_handler('whisper',{'text_normalization':'unknown'})
 def test_historical_evidence_unchanged(self):
  raw=self.raw();result={'raw':raw,'text':raw['text'],'language':'zh'};packet=quality.guarded_inference(lambda _:result,{'start_ms':0,'end_ms':1000},lambda _:None);quality.validate_packet(packet);self.assertEqual(packet['usable_text'],raw['text'])
 def test_tampering_rejected(self):
  raw=self.raw();result={'raw':raw,'text':simplified_chinese(raw['text']),'language':'zh','text_normalization':POLICY};packet=quality.guarded_inference(lambda _:result,{'start_ms':0,'end_ms':1000},lambda _:None);packet['usable_timeline'][0]['text']='伪造';
  with self.assertRaises(ValueError):quality.validate_packet(packet)
 def test_invalid_timestamps_not_repaired_by_text(self):
  raw=self.raw();raw['segments'][0]['end']=-1;result={'raw':raw,'text':raw['text'],'text_normalization':POLICY};self.assertEqual(quality.evaluate(result,0,1000)['usable_text'],'')
if __name__=='__main__':unittest.main()
