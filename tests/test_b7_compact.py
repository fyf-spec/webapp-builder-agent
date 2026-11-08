"""Offline compiler runs exercising request accounting and local gate boundaries."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml
from compact_inventory_fixture import ACCEPTANCE, APP, BUSINESS
from test_b6_compact import requirements
from infrastructure.compact.acceptance import render_suite
from infrastructure.compact.compiler import Budget, CompactCompiler, SPEC
from infrastructure.compact.runtime import install_runtime
from infrastructure.shared.agent.model_policy import inference_policy


class LeanCompact(unittest.TestCase):
    def test_two_call_compile_keeps_original_requirements_and_runs_checks(self):
        root = requirements()
        case = {'expected_status': 'passed', 'outcomes': ['passed'],
                'annotations': [{'type': 'arc-step', 'description': step}
                                for step in ACCEPTANCE['cases'][0]['covers']]}
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {'ARC_B6_MAX_MODEL_CALLS': '2'}):
            directory = Path(tmp)
            (directory / 'requirements.yaml').write_text(yaml.safe_dump(root), encoding='utf8')
            model = Mock()
            model.generate_json.side_effect = [ACCEPTANCE, {'files': [
                {'path': 'backend/src/business/index.ts', 'content': BUSINESS},
                {'path': 'frontend/src/App.tsx', 'content': APP}]}]
            output = directory / 'out'
            compiler = CompactCompiler(directory, output, model=model)
            checks = Mock()
            checks.inventory.return_value = {'returncode': 0, 'cases': [case]}
            checks.typecheck.return_value = {'returncode': 0}
            def build():
                for name in ('backend/dist/server.js', 'frontend/dist/index.html'):
                    artifact = output / name
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.touch()
                return {'returncode': 0}
            checks.build.side_effect = build
            checks.tests.return_value = {'returncode': 0, 'cases': [case], 'passed': 1}
            with patch.object(compiler, 'prepare', side_effect=lambda: install_runtime(output)), \
                    patch('infrastructure.compact.compiler.E2EChecks', return_value=checks):
                compiler.compile()
            self.assertEqual(compiler.report['status'], 'GREEN', compiler.report)
            self.assertEqual(compiler.report['logical_calls'], 2)
            for request in model.generate_json.call_args_list:
                self.assertEqual(request.args[1]['task']['requirements'], root)
            self.assertEqual([r['name'] for r in compiler.report['checks']],
                             ['test_collection', 'test_typecheck', 'build', 'browser_tests'])
            suite = (output / SPEC).read_text(encoding='utf8')
            for step in ACCEPTANCE['cases'][0]['covers']:
                self.assertIn(step, suite)
            receipt = json.loads((output / '.arc/compact-metrics.json').read_text(encoding='utf8'))
            self.assertTrue(all(e['input_tokens'] is None for e in receipt['events']))

    def test_missing_or_invented_step_rejected_before_browser(self):
        with self.assertRaisesRegex(ValueError, 'Missing required outcomes'):
            render_suite(ACCEPTANCE, [*ACCEPTANCE['cases'][0]['covers'], 'NEW'])
        with self.assertRaisesRegex(ValueError, 'original coverage IDs'):
            render_suite(ACCEPTANCE, ['STEP-CREATE'])

    def test_retry_only_wrapped_upstream_reset_and_keep_generation_slot(self):
        reset = RuntimeError("model_proxy_error: connection reset by peer")
        reset.status_code = 400
        with TemporaryDirectory() as tmp:
            for limit, expected in [(3, 2), (2, 1)]:
                model = Mock(side_effect=[reset, {}])
                budget = Budget(Path(tmp), limit)
                if expected == 1:
                    with self.assertRaises(RuntimeError):
                        budget.call('test_author', model, '', {}, reserve=1)
                else:
                    self.assertEqual(budget.call('test_author', model, '', {}, reserve=1), {})
                self.assertEqual(len(budget.events), expected)
            invalid = RuntimeError('400 invalid input')
            invalid.status_code = 400
            model = Mock(side_effect=invalid)
            with self.assertRaises(RuntimeError):
                Budget(Path(tmp), 4).call('joint_application', model, '', {})
            model.assert_called_once()

    def test_application_reasoning_respects_explicit_override(self):
        model = Mock(reasoning='low', max_output_tokens=24000)
        task = {'task': {'phase': 'joint_application'}}
        with patch.dict(os.environ, {'ARC_B5_TASK_REASONING':'1', 'MODEL_REASONING':''}):
            self.assertEqual(inference_policy(task, model)['reasoning'], 'low')
        with patch.dict(os.environ, {'MODEL_REASONING':'none', 'ARC_B5_OVERRIDE_REASONING':'0'}):
            model.reasoning = 'none'
            self.assertEqual(inference_policy(task, model)['reasoning'], 'none')
