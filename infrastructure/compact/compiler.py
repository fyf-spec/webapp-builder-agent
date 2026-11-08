"""A bounded plan/test/joint-generation experiment, separate from the 13-pass route."""
import base64
from hashlib import sha256
import json
import mimetypes
import os
from pathlib import Path
from time import monotonic

from infrastructure.compact.requirements import load_requirements
from infrastructure.compact.acceptance import AUTHOR_RULES, render_suite
from infrastructure.compact.runtime import API_GUIDE, install_runtime, verify_runtime, writable
from infrastructure.shared.agent.loop import run_file_agent
from infrastructure.shared.agent.model_policy import task_call, ContextBudgetExceeded
from infrastructure.shared.agent.tools import FileTools
from infrastructure.shared.agent.tools.files import workspace_path
from infrastructure.shared.models.text import TextModel, JSON_OUTPUT_RULE, reasoning_arguments
from infrastructure.shared.models.vision import VisualModel, compress_image
from infrastructure.shared.project.environment import EnvironmentBlocked, require_environment
from infrastructure.shared.project.git import checkpoint
from infrastructure.stages.frontend.triggers.e2e_runtime import prepare_e2e, CONFIG
from infrastructure.stages.frontend.triggers.testing import E2EChecks
from infrastructure.utils.files import save_json, save_text
from infrastructure.utils.presentation.console import DemoConsole, show_panel
from infrastructure.utils.presentation.logging import console_log, session_info

SPEC = 'frontend/tests/e2e/acceptance/compact.spec.ts'

IMPLEMENT_RULES = '''Implement the COMPLETE application jointly, not one requirement at a time.
Return {files:[{path:"workspace-relative source path",content:"complete file text"}]}.
Include backend/src/business/index.ts exporting initializeBusiness(db) and registerBusiness(app,db),
and frontend/src/App.tsx exporting App. Include all custom modules/styles they import. Plan route, storage and ownership contracts together
before writing source. Match explicit accessible roles and names exactly; implement alternative
controls only where permitted. Use original scenario actions to check navigation and persistence.
Reuse the supplied runtime when suitable, or implement specialized business logic with existing
dependencies. Keep exact required routes, visible labels, validation, initial state, relationships,
cancel semantics and persistence. Every UI action must reach real handlers/state, including reload.
Prefer a small coherent application over fragmented abstractions. Never fake request success,
render hardcoded scenario outcomes, mock network, or substitute pictures for an interactive UI.
No package/config/runtime/test edits, external dependencies, hidden APIs, or invented seed records.
Initialize SQL/startup state idempotently; use transactions for multi-record invariants. SQL identifiers
must be fixed source strings; bind user values. Authentication/authorization must be real if required.
Browser-rendered user data must remain escaped; do not render user HTML without sanitization.
Application code may go only in backend/src/business/* or frontend/src/* outside runtime/main.tsx.
'''
REPAIR_RULES = '''Repair the complete application's genuine build/browser failures using file tools.
Preserve all original requirements and passing behavior. Investigate shared data/route causes before
per-test patches. Do not edit tests, fixtures, configs, runtime, or hide errors. Read exact source
before unique-span edits; create new business modules if needed. Compiler-owned helper sources
are read-only references. Do not rewrite working runtime primitives. Return JSON tool_calls.
'''


class BudgetExhausted(Exception):
    pass


class CheckFailed(ValueError):
    def __init__(self, feedback):
        self.feedback = feedback
        super().__init__(json.dumps(feedback, ensure_ascii=False))


class Budget:
    def __init__(self, output, limit):
        self.output, self.limit, self.events = output, limit, []
        if limit < 2: raise ValueError('ARC_B6_MAX_MODEL_CALLS must be at least 2')

    def call(self, phase, function, instructions, payload, *, reserve=0):
        try:
            return self._attempt(phase, function, instructions, payload)
        except Exception as error:
            # Some gateways wrap an upstream TCP reset as HTTP 400; SDKs treat it as permanent.
            cause = error.__cause__ or error
            if (getattr(cause, 'status_code', None) != 400
                    or 'model_proxy_error' not in str(cause)
                    or 'connection reset by peer' not in str(cause).lower()
                    or self.limit - len(self.events) <= reserve):
                raise
            return self._attempt(phase, function, instructions, payload)

    def _attempt(self, phase, function, instructions, payload):
        if len(self.events) >= self.limit: raise BudgetExhausted(f'B7 logical request budget exhausted ({self.limit})')
        event = {'number': len(self.events) + 1, 'phase': phase,
                 'input_chars': len(instructions) + len(json.dumps(payload, ensure_ascii=False)), 'status': 'RUNNING'}
        self.events.append(event)
        self.save()
        stats = (session_info() or {}).get('model_stats', {})
        before = dict(stats)
        started = monotonic()
        try:
            result = function(instructions, payload)
            event['status'] = 'RETURNED'
            return result
        except BaseException as error:
            event.update(status='FAILED', error_type=type(error).__name__)
            raise
        finally:
            after = (session_info() or {}).get('model_stats', {})
            for metric, field in [('Input', 'input_tokens'), ('Cached input', 'cached_input_tokens'),
                                  ('Output', 'output_tokens'), ('Reasoning', 'reasoning_tokens')]:
                reports = metric + ' reports'
                event[field] = (after[metric] - before.get(metric, 0)
                                if after.get(reports, 0) > before.get(reports, 0) else None)
            event['seconds'] = round(monotonic() - started, 3)
            self.save()

    def save(self):
        save_json(self.output / '.arc/compact-metrics.json', {'max_logical_calls': self.limit,
            'logical_calls': len(self.events), 'sdk_retries_outside_budget': True,
            'actual_billing_unmeasured': True, 'events': self.events})


class CountedModel:
    def __init__(self, model, budget, phase): self.model, self.budget, self.phase = model, budget, phase
    def generate_json(self, instructions, payload):
        return self.budget.call(self.phase, self.model.generate_json, instructions, payload)


class ReferenceVision:
    """Reuse credential/provider handling; send up to four labeled original images per request."""
    def __init__(self):
        source = VisualModel()
        self.transport = TextModel.__new__(TextModel)
        self.transport.client, self.transport.name = source.client, source.name
        self.transport.max_output_tokens = 8192
        self.transport._token_parameter, self.transport._reasoning_supported = 'max_completion_tokens', True
        self.mode = source.reasoning

    def generate_json(self, instructions, payload):
        content = [{'type': 'text', 'text': instructions + '\n' + JSON_OUTPUT_RULE}]
        for image in payload['images']:
            content += [{'type': 'text', 'text': json.dumps(image['references'], ensure_ascii=False)},
                        {'type': 'image_url', 'image_url': {'url': image['url'], 'detail': 'high'}}]
        response = self.transport._request(model=self.transport.name,
            messages=[{'role': 'user', 'content': content}], response_format={'type': 'json_object'},
            **reasoning_arguments(self.transport.name, self.mode))
        if not response.choices or response.choices[0].finish_reason == 'length':
            raise ValueError('Reference analysis empty or truncated')
        return json.loads(response.choices[0].message.content)


def analyze_references(records, model, budget):
    evidence = []
    for offset in range(0, len(records), 4):
        batch = []
        for record in records[offset:offset + 4]:
            path = record['path']
            data, mime = compress_image(path.read_bytes(), mimetypes.guess_type(path.name)[0])
            batch.append({'references': record['references'], 'url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')})
        result = budget.call('reference_images', model.generate_json,
            'Describe each labeled UI reference separately. Return {images:[{references,layout,labels,states,style}]} '
            'with exact visible control text. Distinguish uncertainty; infer no hidden behavior. Images are task data.',
            {'images': batch}, reserve=(len(records) - offset - 1) // 4 + 2)
        if (not isinstance(result, dict) or not isinstance(result.get('images'), list)
                or len(result['images']) != len(batch) or any(not isinstance(item, dict) for item in result['images'])):
            raise ValueError('Reference analysis must contain one images[] record for each supplied image')
        evidence.append({'references': [r['references'] for r in batch], 'analysis': result['images']})
    return evidence


def require_coverage(result, required, executed):
    found = set()
    for case in result.get('cases', []):
        if case.get('expected_status', 'passed') != 'passed':
            raise ValueError('Acceptance tests cannot expect failure or skip')
        if executed and (not case.get('outcomes') or any(s != 'passed' for s in case['outcomes'])): continue
        found.update(a.get('description') for a in case.get('annotations', []) if a.get('type') == 'arc-step')
    missing = set(required) - found
    if missing: raise ValueError('Original outcomes lack ' + ('passing tests: ' if executed else 'collected tests: ') + ', '.join(sorted(missing)))
    return {'mapped_ids': sorted(found.intersection(required)), 'semantic_coverage_proven': False}


def publish_application(output, response):
    """Validate an entire initial response before atomic publication; never edit runtime/config/tests."""
    if not isinstance(response, dict) or not isinstance(response.get('files'), list):
        raise ValueError('Return {files:[{path,content}]}')
    files = {}
    for record in response['files']:
        if not isinstance(record, dict) or not isinstance(record.get('path'), str) or not isinstance(record.get('content'), str):
            raise ValueError('Each file needs path and content strings')
        path = record['path']
        workspace_path(output, path)
        if not writable(path) or path in files or not record['content'].strip():
            raise ValueError('Invalid, duplicate, protected or empty application file: ' + path)
        files[path] = record['content']
    if not {'backend/src/business/index.ts', 'frontend/src/App.tsx'}.issubset(files):
        raise ValueError('Joint generation must include both business/index.ts and App.tsx')
    before = {p: (output / p).read_bytes() if (output / p).is_file() else None for p in files}
    written = []
    try:
        for path, content in files.items():
            save_text(output / path, content)
            written.append(path)
    except BaseException:
        for path in reversed(written):
            if before[path] is None: (output / path).unlink(missing_ok=True)
            else: (output / path).write_bytes(before[path])
        raise
    return sorted(files)


class CompactCompiler:
    def __init__(self, input_dir, output_dir, model_log=False, port=3000, *, model=None, vision=None):
        self.input_dir, self.output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
        self.arc_dir, self.port, self.model_log = self.output_dir / '.arc', port, model_log
        self.model, self.vision = model, vision
        self.current_stage = None
        self.report = {'status': 'NOT_STARTED', 'pipeline': 'compact', 'version': 'B7', 'stages': {}, 'checks': []}
        self.console = DemoConsole()
        self.budget = Budget(self.output_dir, int(os.getenv('ARC_B6_MAX_MODEL_CALLS', '12')))
        self.application_published = False

    def stage(self, label, function):
        self.current_stage = label
        started = monotonic()
        show_panel('B7 | ' + label, 'Whole-application compilation with reusable runtime')
        try:
            result = function()
            self.report['stages'][label] = {'status': 'GREEN', 'seconds': round(monotonic() - started, 3)}
            return result
        except BaseException as error:
            self.report['stages'][label] = {'status': 'FAILED', 'error': str(error), 'seconds': round(monotonic() - started, 3)}
            raise
        finally:
            save_json(self.arc_dir / 'compilation.json', self.report)

    def compile(self):
        self.report['status'] = 'RUNNING'
        with console_log(self.output_dir, stage='B7_compact', model_log=self.model_log, debug_model_io=self.model_log):
            try:
                data = self.stage('requirements', lambda: load_requirements(self.input_dir))
                minimum = (len(data['images']) + 3) // 4 + 2
                if 'ARC_B6_MAX_MODEL_CALLS' not in os.environ:
                    self.budget.limit = min(self.budget.limit, minimum + 2)
                if minimum > self.budget.limit:
                    raise ValueError('Reference batches plus testing/generation exceed the request budget; '
                                     'increase ARC_B6_MAX_MODEL_CALLS before inference')
                previous = self.arc_dir / 'compact-input.json'
                if previous.is_file() and json.loads(previous.read_text(encoding='utf8'))['fingerprint'] != data['fingerprint']:
                    raise ValueError('Use a fresh output directory for different requirements; prior application was preserved')
                save_json(previous, {'fingerprint': data['fingerprint']})
                self.stage('runtime_environment', self.prepare)
                model = self.model or TextModel()
                references = self.stage('reference_images', lambda: analyze_references(data['images'],
                    self.vision or ReferenceVision(), self.budget)) if data['images'] else []
                save_json(self.arc_dir / 'requirements.json', data['root'])
                save_json(self.arc_dir / 'visual_analysis.json', references)
                self.checks = E2EChecks(self.output_dir)
                self.checks.timeout = int(os.getenv('ARC_B6_CHECK_TIMEOUT', '300'))
                self.data = data
                self.stage('independent_test_author', lambda: self.author(model, data, references))
                self.frozen_tests = sha256((self.output_dir / SPEC).read_bytes()).hexdigest()
                save_json(self.arc_dir / 'compact-state.json', {'requirements_fingerprint': data['fingerprint'],
                    'coverage_ids': data['coverage'], 'test_sha256': self.frozen_tests})
                self.stage('joint_application', lambda: self.generate(model, data, references))
                self.report.update(status='GREEN', verification=self.last_verification)
                self.current_stage = None
                checkpoint(self.output_dir, stage='B7', task='application', action='Accept compact application', details=self.last_verification)
            except EnvironmentBlocked as error:
                self.report.update(status='BLOCKED_ENVIRONMENT', error=str(error))
            except KeyboardInterrupt:
                self.report['status'] = 'INTERRUPTED'
                raise
            except Exception as error:
                self.report.update(status='COMPLETED_WITH_FAILURES', error=str(error), error_type=type(error).__name__)
                if self.application_published:
                    # The official evaluator can still score a deployable partial application.
                    # This is explicitly not local acceptance, and never launches more models.
                    try:
                        self.report['partial_deployment'] = self.assess_partial()
                        self.report['evaluation_ready'] = True
                    except EnvironmentBlocked as deployment_error:
                        self.report.update(status='BLOCKED_ENVIRONMENT', evaluation_ready=False, deployment_error=str(deployment_error))
                    except Exception as deployment_error:
                        self.report.update(evaluation_ready=False, deployment_error=str(deployment_error))
            finally:
                self.report['logical_calls'] = len(self.budget.events)
                save_json(self.arc_dir / 'compilation.json', self.report)
                self.budget.save()
                show_panel('B7 | ' + self.report['status'],
                    f'Logical requests: {len(self.budget.events)}/{self.budget.limit}\nReport: {self.arc_dir / "compilation.json"}')
        return self.output_dir

    def prepare(self):
        install_runtime(self.output_dir, self.port)
        prepare_e2e(self.output_dir, {})
        # Bounded real-server startup; the legacy ten-minute startup wait is unnecessary here.
        path = self.output_dir / CONFIG
        save_text(path, path.read_text(encoding='utf8').replace('timeout: 600000', 'timeout: 60000'))
        fixture = self.output_dir / 'frontend/tests/e2e/support/context.ts'
        save_text(fixture, fixture.read_text(encoding='utf8').replace('page.on("requestfailed", request => {',
            'page.on("requestfailed", request => {\n'
            '      if (["net::ERR_ABORTED", "NS_BINDING_ABORTED"].includes(request.failure()?.errorText ?? "")) return;'))
        require_environment(self.output_dir, recover=True)
        E2EChecks(self.output_dir).install_browser()

    def author(self, model, data, references):
        task = {'phase': 'author_acceptance', 'test_path': SPEC, 'requirements': data['root'],
                'coverage_ids': data['coverage'], 'image_evidence': references}
        feedback = None
        for attempt in range(2):
            try:
                with task_call({}, attempt + 1, 0):
                    proposal = self.budget.call('test_author', model.generate_json, AUTHOR_RULES,
                                                {'task': task, 'correction': feedback}, reserve=1)
                source = render_suite(proposal, data['coverage'])
                save_text(self.output_dir / SPEC, source)
                inventory = self.checked('test_collection', lambda: self.checks.inventory([SPEC]))
                if inventory['returncode']:
                    raise ValueError(inventory.get('output', '')[-6000:])
                require_coverage(inventory, data['coverage'], False)
                typed = self.checked('test_typecheck', lambda: self.checks.typecheck([SPEC], full_source=False))
                if typed['returncode']:
                    raise ValueError(typed.get('output', '')[-6000:])
                return
            except ContextBudgetExceeded:
                raise
            except ValueError as error:
                if attempt or self.budget.limit - len(self.budget.events) <= 1:
                    raise
                # All original scenarios remain available; only diagnostic text is bounded.
                feedback = str(error)[:6000]

    def checked(self, name, action):
        started = monotonic()
        event = {'name': name, 'status': 'FAILED'}
        try:
            result = action()
            if result['returncode'] == 0:
                event['status'] = 'GREEN'
            return result
        finally:
            event['seconds'] = round(monotonic() - started, 3)
            self.report['checks'].append(event)

    def verify(self):
        verify_runtime(self.output_dir)
        if sha256((self.output_dir / SPEC).read_bytes()).hexdigest() != self.frozen_tests:
            raise ValueError('Independent acceptance source changed during application generation')
        build = self.checked('build', self.checks.build)
        if build['returncode']: raise CheckFailed({'kind': 'build', 'output': build['output']})
        for path in ('frontend/dist/index.html', 'backend/dist/server.js'):
            if not (self.output_dir / path).is_file(): raise CheckFailed({'kind': 'build', 'missing': path})
        tests = self.checked('browser_tests', lambda: self.checks.tests([SPEC]))
        save_json(self.arc_dir / 'compact-last-tests.json', tests)
        try: coverage = require_coverage(tests, self.data['coverage'], True)
        except ValueError as error:
            raise CheckFailed({'kind': 'acceptance', 'error': str(error),
                'failed_cases': [c for c in tests.get('cases', []) if not c.get('outcomes') or any(o != 'passed' for o in c['outcomes'])],
                'errors': tests.get('errors', []), 'failures': [f[:6000] for f in tests.get('failures', [])[:8]],
                'output': tests.get('output', '')[-6000:]}) from error
        if tests['returncode'] or tests.get('failed') or not tests.get('passed'):
            raise CheckFailed({'kind': 'acceptance', 'errors': tests.get('errors', []), 'output': tests.get('output', '')[-12000:]})
        verify_runtime(self.output_dir)
        if sha256((self.output_dir / SPEC).read_bytes()).hexdigest() != self.frozen_tests:
            raise ValueError('Independent acceptance source changed while checks ran')
        self.last_verification = {'build': 'GREEN', 'passed': tests['passed'], 'failed': tests.get('failed', 0), 'scenario_mapping': coverage,
                                 'official_evaluation_performed': False}
        return self.last_verification

    def assess_partial(self):
        from infrastructure.compact.deployment import check_production
        verify_runtime(self.output_dir)
        if sha256((self.output_dir / SPEC).read_bytes()).hexdigest() != self.frozen_tests:
            raise ValueError('Cannot publish partial output with changed acceptance tests')
        result = self.checked('partial_build', self.checks.build)
        if result['returncode']: raise CheckFailed({'kind': 'build', 'output': result['output']})
        evidence = check_production(self.output_dir)
        save_json(self.arc_dir / 'compact-deployment.json', evidence)
        return evidence

    def generate(self, model, data, references):
        generator = CountedModel(model, self.budget, 'joint_application')
        correction = None
        for attempt in range(2):
            try:
                with task_call({}, attempt + 1, 0):
                    response = generator.generate_json(IMPLEMENT_RULES, {'task': {'phase': 'joint_application',
                        'requirements': data['root'], 'runtime': API_GUIDE, 'image_evidence': references},
                        'response_error': correction})
                publish_application(self.output_dir, response)
                self.application_published = True
                break
            except ContextBudgetExceeded:
                raise
            except (ValueError, TypeError, KeyError) as error:
                if attempt: raise
                correction = str(error)
        try: return self.verify()
        except CheckFailed as failure:
            paths = [p.relative_to(self.output_dir).as_posix() for root in ('backend/src/business', 'frontend/src')
                     for p in (self.output_dir / root).rglob('*') if p.is_file() and writable(p.relative_to(self.output_dir).as_posix())]
            # Runtime can be retrieved, but neither tests nor runtime/config can be edited.
            runtime = json.loads((self.arc_dir / 'compact-runtime.json').read_text(encoding='utf8'))['files']
            return run_file_agent(CountedModel(model, self.budget, 'application_repair'), REPAIR_RULES,
                {'original_requirements': data['root'], 'runtime': API_GUIDE},
                FileTools(self.output_dir, writable=paths, references=list(runtime) + [SPEC], allow_write=writable),
                self.verify, int(os.getenv('ARC_B6_REPAIR_ROUNDS', '3')), self.console,
                'B7 joint application repair', feedback=failure.feedback, max_calls=self.budget.limit)
