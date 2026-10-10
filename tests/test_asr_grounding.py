import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import urllib.error

from primebuddy.voice_support.qwen_asr import request_payload, recognize, MODEL, NEW_MODEL, AsrError
from primebuddy.grounding import confirmation_for, confirmation_answer, vocabulary
from primebuddy.registry import Registry

CONFIG = {'endpoint': 'https://example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions',
          'api_key': 'test-only', 'model': MODEL}


class AsrTests(unittest.TestCase):
    def test_new_model_uses_native_endpoint_and_real_name_vocabulary(self):
        url, body = request_payload('data:audio/wav;base64,AA==', CONFIG, NEW_MODEL,
                                    ['元仔', '天际线灯', '天际线灯', '长' * 16])
        self.assertTrue(url.endswith('/api/v1/services/aigc/multimodal-generation/generation'))
        self.assertEqual(body['parameters']['vocabulary'], {'元仔': 4, '天际线灯': 4})
        self.assertEqual(body['input']['messages'][-1]['content'][0]['type'], 'input_audio')
        self.assertLessEqual(len(body['input']['messages'][0]['content'][0]['text']), 400)

    def test_old_model_keeps_compatible_contract(self):
        url, body = request_payload('audio', CONFIG, MODEL, ['天际线灯'])
        self.assertEqual(url, CONFIG['endpoint'])
        self.assertEqual(body['model'], MODEL)
        self.assertNotIn('parameters', body)

    def test_new_response_text_is_parsed(self):
        response = Mock()
        response.read.return_value = json.dumps({'output': {'text': '关闭天际线灯。'}})
        with patch('primebuddy.voice_support.qwen_asr.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertEqual(recognize('audio', CONFIG, NEW_MODEL), '关闭天际线灯。')

    def test_model_permission_failure_does_not_fallback_or_retry(self):
        with patch('primebuddy.voice_support.qwen_asr.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError('url', 403, 'blocked', {}, None)
            with self.assertRaises(AsrError):
                recognize('audio', CONFIG, NEW_MODEL)
            opener.return_value.open.assert_called_once()


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry(Path(__file__).resolve().parents[2] / 'primebuddy-skills')
        self.snapshot = {'yeelight': {'status': 'success', 'rooms': ['客厅'], 'vocabulary': ['客厅', '天际线灯'],
                                     'devices': [{'name': '天际线灯', 'room': '客厅'}]}}
        self.plan = {'reply': '', 'tasks': [{'id': 'light', 'skill': 'yeelight', 'action': 'set_power',
            'args': {'power': False, 'scope': 'device', 'room': '', 'device': '天际线灯'}, 'after': []}]}

    def test_exact_name_does_not_need_extra_confirmation(self):
        self.assertIsNone(confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot))

    def test_phonetic_candidate_requires_confirmation(self):
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯', self.snapshot)
        self.assertIn('关闭天际线灯', question)
        self.assertIn('确认', question)

    def test_unknown_candidate_and_duplicate_names_are_blocked(self):
        self.plan['tasks'][0]['args']['device'] = '不存在的灯'
        with self.assertRaises(ValueError):
            confirmation_for(self.plan, self.registry, '关闭不存在的灯', self.snapshot)
        self.plan['tasks'][0]['args']['device'] = '天际线灯'
        self.snapshot['yeelight']['devices'].append({'name': '天际线灯', 'room': '厨房'})
        with self.assertRaises(ValueError):
            confirmation_for(self.plan, self.registry, '关闭天际线灯', self.snapshot)

    def test_full_plan_is_named_in_confirmation(self):
        self.plan['tasks'].append({'id': 'music', 'skill': 'diangetai', 'action': 'play',
                                  'args': {'artist': '周杰伦', 'song': ''}, 'after': []})
        question = confirmation_for(self.plan, self.registry, '关闭天气限灯并播放周杰伦', self.snapshot)
        self.assertIn('周杰伦', question)

    def test_confirmation_accepts_only_explicit_answers(self):
        self.assertIs(confirmation_answer('确认。'), True)
        self.assertIs(confirmation_answer('不是'), False)
        self.assertIsNone(confirmation_answer('是的，但是换成厨房灯'))

    def test_hotwords_come_from_actual_inventory(self):
        self.assertIn('天际线灯', vocabulary(self.snapshot))
        self.assertNotIn('天气限灯', vocabulary(self.snapshot))
