"""Compact-route contracts, immutable runtime, and bounded request failure injection."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import yaml
from infrastructure.compact.compiler import (Budget, BudgetExhausted, CountedModel, CompactCompiler,
    publish_application, require_coverage, analyze_references, SPEC)
from infrastructure.compact.requirements import load_requirements, validate_plan
from infrastructure.compact.runtime import install_runtime, verify_runtime, writable


def requirements():
    return {'id': 'ROOT', 'name': 'Inventory', 'children': [
        {'id': 'R-CREATE', 'name': 'Create item', 'description': 'Item has a name; initially empty.', 'scenarios': [
            {'id': 'S-CREATE', 'steps': [{'id': 'STEP-CREATE', 'Given': 'Inventory is empty',
                'When': 'Create an item named Blue pen', 'Then': 'It is listed and survives reload'}]}]},
        {'id': 'R-EDIT', 'name': 'Edit item', 'scenarios': [{'id': 'S-EDIT', 'steps': [
            {'id': 'STEP-EDIT', 'Given': 'An item exists', 'When': 'Open, rename and save it', 'Then': 'New name persists'}]}]},
        {'id': 'R-DELETE', 'name': 'Cancel or confirm deletion', 'scenarios': [{'id': 'S-DELETE', 'steps': [
            {'id': 'STEP-DELETE', 'Given': 'An item exists', 'When': 'Cancel deletion then confirm deletion',
             'Then': 'Cancel preserves it; confirmed deletion removes it'}]}]}]}


def plan():
    return {'entities': [{'name': 'Item', 'fields': ['name']}], 'routes': [{'path': '/'}, {'path': '/items/:id'}],
            'workflows': [{'requirement_ids': ['R-CREATE', 'R-EDIT', 'R-DELETE'], 'summary': 'Create, edit, delete'}],
            'requirements': {rid: 'Implement ' + rid for rid in ('ROOT', 'R-CREATE', 'R-EDIT', 'R-DELETE')}}


class CompactMechanisms(unittest.TestCase):
    def test_cli_defaults_to_compact_and_returns_failure_without_silent_fallback(self):
        import main
        import os
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'input'; source.mkdir()
            (source / 'requirements.yaml').write_text(yaml.safe_dump(requirements()), encoding='utf8')
            args = [str(source), '-o', str(Path(directory) / 'output')]
            with patch.dict(os.environ, {'ARC_PIPELINE': 'compact'}), \
                    patch('infrastructure.compact.compiler.CompactCompiler') as compact, patch.object(main, 'Compiler') as classic:
                compact.return_value.report = {'status': 'COMPLETED_WITH_FAILURES'}
                with self.assertRaises(SystemExit) as error: main.main(args)
                self.assertEqual(error.exception.code, 1)
                compact.assert_called_once(); classic.assert_not_called()
                self.assertEqual(main.parse_args(args).pipeline, 'compact')
                compact.return_value.report['evaluation_ready'] = True
                with self.assertRaises(SystemExit) as partial: main.main(args)
                self.assertEqual(partial.exception.code, 0)
                self.assertEqual(compact.return_value.report['status'], 'COMPLETED_WITH_FAILURES')

    def test_runtime_idempotency_protection_and_custom_sources_preserved(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            installed = install_runtime(output)
            self.assertIn('backend/src/runtime/store.ts', installed)
            custom = output / 'backend/src/business/custom.ts'
            custom.write_text('export const value = 42;', encoding='utf8')
            install_runtime(output)
            verify_runtime(output)
            self.assertEqual(custom.read_text(), 'export const value = 42;')
            store = output / 'backend/src/runtime/store.ts'
            store.write_text('custom runtime', encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'runtime changed'): verify_runtime(output)
            with self.assertRaisesRegex(ValueError, 'custom runtime'): install_runtime(output)
            self.assertEqual(store.read_text(), 'custom runtime')

    def test_first_generation_atomic_validation_does_not_overwrite_config_or_tests(self):
        with TemporaryDirectory() as directory:
            output = Path(directory); install_runtime(output)
            before = (output / 'frontend/src/App.tsx').read_bytes()
            for path in ('../escape.ts', 'frontend/src/runtime/ui.tsx', 'backend/src/runtime/store.ts',
                         'package.json', SPEC, 'frontend/src/main.tsx', 'frontend/src/../runtime/ui.tsx'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    publish_application(output, {'files': [{'path': 'frontend/src/App.tsx', 'content': 'changed'},
                                                          {'path': path, 'content': 'no'}]})
                self.assertEqual((output / 'frontend/src/App.tsx').read_bytes(), before)
            self.assertFalse(writable('backend/src/api/register.ts'))

    def test_publication_rolls_back_partial_disk_failure(self):
        with TemporaryDirectory() as directory:
            output = Path(directory); install_runtime(output)
            app = output / 'frontend/src/App.tsx'; before = app.read_bytes()
            import infrastructure.compact.compiler as module
            real = module.save_text
            def save(path, text):
                if path.name == 'index.ts': raise OSError('injected disk error')
                real(path, text)
            with patch.object(module, 'save_text', side_effect=save), self.assertRaises(OSError):
                publish_application(output, {'files': [{'path': 'frontend/src/App.tsx', 'content': 'new'},
                    {'path': 'backend/src/business/index.ts', 'content': 'new'}]})
            self.assertEqual(app.read_bytes(), before)

    def test_budget_counts_failed_calls_and_blocks_before_transport(self):
        with TemporaryDirectory() as directory:
            model = Mock(); model.generate_json.side_effect = ValueError('bad JSON')
            budget = Budget(Path(directory), 3); wrapped = CountedModel(model, budget, 'repair')
            for _ in range(3):
                with self.assertRaises(ValueError): wrapped.generate_json('rules', {})
            with self.assertRaises(BudgetExhausted): wrapped.generate_json('rules', {})
            self.assertEqual(model.generate_json.call_count, 3)
            receipt = json.loads((Path(directory) / '.arc/compact-metrics.json').read_text())
            self.assertEqual(receipt['logical_calls'], 3)
            self.assertTrue(all(e['status'] == 'FAILED' for e in receipt['events']))

    def test_scenario_mapping_rejects_missing_skipped_failed_and_expected_failure(self):
        good = {'annotations': [{'type': 'arc-step', 'description': 'STEP-CREATE'}],
                'expected_status': 'passed', 'outcomes': ['passed'], 'asserted_ids_by_run': [['STEP-CREATE']]}
        self.assertFalse(require_coverage({'cases': [good]}, ['STEP-CREATE'], True)['semantic_coverage_proven'])
        for changes in ({'outcomes': ['skipped']}, {'outcomes': []}, {'outcomes': ['failed']},
                        {'expected_status': 'failed'}, {'annotations': []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                require_coverage({'cases': [{**good, **changes}]}, ['STEP-CREATE'], True)

    def test_description_only_leaves_duplicate_ids_and_plan_omissions(self):
        with TemporaryDirectory() as directory:
            root = requirements(); root['children'].append({'id': 'HELP', 'name': 'Help', 'description': 'Show instructions'})
            path = Path(directory) / 'requirements.yaml'
            path.write_text(yaml.safe_dump(root), encoding='utf8')
            data = load_requirements(Path(directory))
            self.assertIn('requirement:HELP', data['coverage'])
            with self.assertRaisesRegex(ValueError, 'every original'): validate_plan(plan(), data['tree'].requirements)
            root['children'].append(root['children'][0]); path.write_text(yaml.safe_dump(root), encoding='utf8')
            with self.assertRaisesRegex(ValueError, 'Duplicate requirement'): load_requirements(Path(directory))

    def test_environment_failure_stops_before_any_model_call(self):
        from infrastructure.shared.project.environment import EnvironmentBlocked
        with TemporaryDirectory() as directory:
            root = Path(directory); source = root / 'input'; source.mkdir()
            (source / 'requirements.yaml').write_text(yaml.safe_dump(requirements()), encoding='utf8')
            model = Mock()
            compiler = CompactCompiler(source, root / 'output', model=model)
            with patch.object(compiler, 'prepare', side_effect=EnvironmentBlocked('injected missing dependency')):
                compiler.compile()
            self.assertEqual(compiler.report['status'], 'BLOCKED_ENVIRONMENT')
            self.assertEqual(len(compiler.budget.events), 0)
            model.generate_json.assert_not_called()

    def test_reference_images_deduplicate_and_batch_without_dropping_owners(self):
        from PIL import Image
        with TemporaryDirectory() as directory:
            source = Path(directory); Image.new('RGB', (16, 16), 'white').save(source / 'reference.png')
            root = requirements()
            for child in root['children']:
                child['description'] = {'text': 'Visible screen', 'image': [{'id': child['id'], 'reference': 'reference.png'}]}
            (source / 'requirements.yaml').write_text(yaml.safe_dump(root), encoding='utf8')
            data = load_requirements(source)
            self.assertEqual(len(data['images']), 1)
            model = Mock(); model.generate_json.return_value = {'images': [{'labels': ['Create']} ]}
            evidence = analyze_references(data['images'], model, Budget(source, 3))
            self.assertEqual(len(evidence[0]['references'][0]), 3)
            model.generate_json.assert_called_once()


if __name__ == '__main__': unittest.main()
