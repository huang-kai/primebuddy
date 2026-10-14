"""工具循环回归：按需加载、执行回传、确认前暂停和失败后不重试。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from primebuddy.config.storage import write_config
from primebuddy.core.engine import Context, execute
from primebuddy.core.planner import continue_plan, plan
from primebuddy.core.grounding import confirmation_for
from primebuddy.skills.registry import Registry


def tool(name, args, identifier):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': identifier, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / 'hello'
        (source / 'src').mkdir(parents=True)
        (source / 'cfg').mkdir()
        (source / 'SKILL.md').write_text('---\nname: hello\ndescription: 问候\n---\n仅被选中后才加载的正文。')
        write_config(source / 'cfg/pkg.yaml', {'primebuddy': {'entrypoint': 'src/actions.py', 'actions': {
            'hello': {'parameters': {'name': {'type': 'string'}}, 'resources': []}}}})
        (source / 'src/actions.py').write_text('''def hello(context, name):
    context.checkpoint()
    return '你好，' + name

def planning_context(context):
    return {'status': 'success', 'vocabulary': ['世界']}
''')
        self.registry = Registry(self.root)
        self.context = Context()
        self.task = {'id': 'one', 'skill': 'hello', 'action': 'hello', 'args': {'name': '世界'}, 'after': []}

    def responses(self):
        return [tool('read_skill_file', {'skill': 'hello', 'path': 'SKILL.md'}, 'read'),
                tool('get_skill_context', {'skill': 'hello'}, 'context'),
                tool('execute_tasks', {'tasks': [self.task]}, 'execute')]

    def test_minimal_native_function_runs_without_five_hooks_and_results_return_to_model(self):
        messages = []
        responses = iter([*self.responses(), {'role': 'assistant', 'content': '还想做什么？'}])
        def chat(history, tools, config):
            messages.append(copy.deepcopy(history))
            return next(responses)
        snapshots = {}
        with patch('primebuddy.core.planner.chat', side_effect=chat):
            proposal = plan('问候世界', self.registry, skill_context=snapshots, context=self.context)
            self.assertNotIn('仅被选中后', messages[0][0]['content'])
            self.assertEqual(self.registry.modules.keys(), {'hello'})
            self.assertEqual(snapshots['hello']['vocabulary'], ['世界'])
            result = execute(proposal, self.registry, self.context, lambda _: None)
            final = continue_plan(proposal, result, self.registry)
        self.assertEqual(result['one']['message'], '你好，世界')
        self.assertEqual(final, {'reply': '还想做什么？', 'tasks': []})
        response = messages[-1][-1]
        self.assertEqual((response['role'], response['tool_call_id']), ('tool', 'execute'))
        self.assertEqual(json.loads(response['content'])['one']['message'], '你好，世界')

    def test_plain_instruction_skill_works_without_executable_code(self):
        (self.root / 'hello/cfg/pkg.yaml').unlink()
        (self.root / 'hello/src/actions.py').unlink()
        registry = Registry(self.root)
        with patch('primebuddy.core.planner.chat', side_effect=[
                tool('read_skill_file', {'skill': 'hello', 'path': 'SKILL.md'}, 'read'),
                {'role': 'assistant', 'content': '你好。'}]):
            self.assertEqual(plan('你好', registry), {'reply': '你好。', 'tasks': []})
        self.assertEqual(registry.modules, {})

    def test_skill_must_be_read_before_any_execution(self):
        with patch('primebuddy.core.planner.chat', return_value=tool('execute_tasks', {'tasks': [self.task]}, 'execute')), \
                patch('primebuddy.skills.manager.load_entry') as load:
            with self.assertRaisesRegex(ValueError, '读取'):
                plan('问候世界', self.registry)
        load.assert_not_called()

    def test_reference_and_context_reads_are_scoped_to_activated_skill(self):
        for message in (tool('read_skill_file', {'skill': 'hello', 'path': 'src/actions.py'}, 'read'),
                        tool('get_skill_context', {'skill': 'hello'}, 'context'),
                        tool('read_skill_file', {'skill': 'missing', 'path': 'SKILL.md'}, 'missing')):
            with self.subTest(message=message), patch('primebuddy.core.planner.chat', return_value=message), self.assertRaises(ValueError):
                plan('测试', self.registry)

    def test_invalid_arguments_are_rejected_before_any_execution(self):
        task = dict(self.task, args={'name': '世界', 'command': 'unexpected'})
        with patch('primebuddy.core.planner.chat', side_effect=[self.responses()[0], tool('execute_tasks', {'tasks': [task]}, 'execute')]), \
                self.assertRaisesRegex(ValueError, '未知字段'):
            plan('测试', self.registry)

    def test_unknown_and_multiple_tool_calls_cannot_execute(self):
        unknown = tool('shell', {'command': 'anything'}, 'unknown')
        multiple = tool('execute_tasks', {'tasks': [self.task]}, 'execute')
        multiple['tool_calls'].append(unknown['tool_calls'][0])
        for message in (unknown, multiple):
            with self.subTest(message=message), patch('primebuddy.core.planner.chat', return_value=message), self.assertRaises(ValueError):
                plan('测试', self.registry)

    def test_confirmation_pauses_actual_write_and_resume_keeps_same_tool_call(self):
        module = self.registry.load('hello').native_module()
        module.confirmation = lambda *args: '需确认'
        with patch('primebuddy.core.planner.chat', side_effect=[*self.responses(), {'role': 'assistant', 'content': '完成'}]):
            proposal = plan('问候世界', self.registry, context=self.context)
            with patch.object(module, 'hello', wraps=module.hello) as action:
                self.assertIn('确认', confirmation_for(proposal, self.registry, '问候世界', {}))
                action.assert_not_called()
                self.assertEqual(proposal.call_id, 'execute')
                results = execute(proposal, self.registry, self.context, lambda _: None)
                continue_plan(proposal, results, self.registry)
                action.assert_called_once()

    def test_failed_result_is_returned_but_cannot_trigger_automatic_retry(self):
        retry = tool('execute_tasks', {'tasks': [dict(self.task, id='retry')]}, 'retry')
        with patch('primebuddy.core.planner.chat', side_effect=[*self.responses(), retry]):
            proposal = plan('测试', self.registry)
            with self.assertRaisesRegex(ValueError, '未成功'):
                continue_plan(proposal, {'one': {'status': 'failed', 'message': '状态未确认'}}, self.registry)

    def test_successful_action_is_not_repeated_by_followup_model_call(self):
        retry = tool('execute_tasks', {'tasks': [dict(self.task, id='repeat')]}, 'repeat')
        with patch('primebuddy.core.planner.chat', side_effect=[*self.responses(), retry]):
            proposal = plan('测试', self.registry)
            results = execute(proposal, self.registry, self.context, lambda _: None)
            with self.assertRaisesRegex(ValueError, '重复'):
                continue_plan(proposal, results, self.registry)

    def test_followup_step_can_use_previous_real_result(self):
        second = dict(self.task, id='second', args={'name': '第二位'})
        with patch('primebuddy.core.planner.chat', side_effect=[*self.responses(),
                tool('execute_tasks', {'tasks': [second]}, 'second'), {'role': 'assistant', 'content': '完成'}]):
            proposal = plan('分别问候', self.registry)
            proposal = continue_plan(proposal, execute(proposal, self.registry, self.context, lambda _: None), self.registry)
            result = execute(proposal, self.registry, self.context, lambda _: None)
            final = continue_plan(proposal, result, self.registry)
        self.assertEqual(result['second']['message'], '你好，第二位')
        self.assertEqual(final['tasks'], [])

    def test_runaway_read_loop_is_bounded(self):
        calls = iter(tool('read_skill_file', {'skill': 'hello', 'path': 'SKILL.md'}, str(i)) for i in range(20))
        with patch('primebuddy.core.planner.chat', side_effect=lambda *args: next(calls)) as chat, self.assertRaisesRegex(ValueError, '十二次'):
            plan('测试', self.registry)
        self.assertEqual(chat.call_count, 12)


class ProviderToolTests(unittest.TestCase):
    CONFIG = {'model': 'qwen-plus', 'endpoint': 'https://example.invalid/chat/completions', 'api_key': 'fixture-only'}

    def test_qwen_tools_and_tool_history_use_native_protocol_without_json_response_format(self):
        from primebuddy.providers.qianwen.llm import chat
        response = Mock()
        response.read.return_value = json.dumps({'choices': [{'finish_reason': 'tool_calls',
            'message': tool('read_skill_file', {'skill': 'hello', 'path': 'SKILL.md'}, 'read')}]})
        history = [{'role': 'tool', 'tool_call_id': 'previous', 'content': '真实结果'}]
        with patch('primebuddy.providers.qianwen.llm.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value = response
            result = chat(history, [{'type': 'function', 'function': {'name': 'read_skill_file'}}], self.CONFIG)
            request = opener.return_value.open.call_args.args[0]
            body = json.loads(request.data)
        self.assertIn('tools', body)
        self.assertNotIn('response_format', body)
        self.assertFalse(body['parallel_tool_calls'])
        self.assertEqual(body['messages'], history)
        self.assertEqual(result['tool_calls'][0]['id'], 'read')

    def test_qwen_incomplete_output_is_rejected_before_tool_execution(self):
        from primebuddy.providers.qianwen.llm import chat
        response = Mock()
        response.read.return_value = json.dumps({'choices': [{'finish_reason': 'length', 'message': {}}]})
        with patch('primebuddy.providers.qianwen.llm.urllib.request.build_opener') as opener, self.assertRaisesRegex(ValueError, '不完整'):
            opener.return_value.open.return_value.__enter__.return_value = response
            chat([], [], self.CONFIG)
