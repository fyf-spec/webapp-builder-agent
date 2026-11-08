"""Real compilation, browser, HTTP, persistence and fault recovery; scripted inference only."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml
from compact_inventory_fixture import BUSINESS, APP, ACCEPTANCE, IDS
from test_b6_compact import requirements
from infrastructure.compact.compiler import CheckFailed, CompactCompiler, require_coverage, SPEC
from infrastructure.compact.acceptance import render_suite
from infrastructure.compact.runtime import verify_runtime
from infrastructure.shared.models.text import TextModel
from infrastructure.stages.frontend.triggers.testing import E2EChecks
from infrastructure.stages.frontend.ui.testing import parse_playwright_result
from infrastructure.utils.processes import capture_process


class ReusableRuntimeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        modules = os.getenv('ARC_CONTRACT_TEST_NODE_MODULES')
        if not modules or not shutil.which('node'): raise unittest.SkipTest('Set ARC_CONTRACT_TEST_NODE_MODULES to trusted installed dependencies')
        cls.modules = Path(modules).resolve()

    def link_modules(self, output):
        link = output / 'node_modules'
        if os.name == 'nt':
            subprocess.run(['powershell', '-NoProfile', '-Command',
                'New-Item -ItemType Junction -Path $env:ARC_TEST_LINK -Target $env:ARC_TEST_MODULES | Out-Null'],
                env={**os.environ, 'ARC_TEST_LINK': str(link), 'ARC_TEST_MODULES': str(self.modules)}, check=True)
        else:
            link.symlink_to(self.modules, target_is_directory=True)
        return link

    def test_passing_navigation_only_test_has_no_executed_coverage(self):
        output = Path(self.enterContext(TemporaryDirectory()))
        junction = self.link_modules(output)
        self.addCleanup(os.rmdir if os.name == 'nt' else Path.unlink, junction)
        (output / 'old.spec.ts').write_text(
            "import {test} from '@playwright/test';\n"
            "test('navigation only', {annotation:{type:'arc-step',description:'STEP-CREATE'}}, "
            "async ({page}) => {await page.goto('data:text/html,inventory');});\n", encoding='utf8')
        result = E2EChecks(output).execute(['node_modules/@playwright/test/cli.js', 'test',
                                            'old.spec.ts', '--reporter=json'])
        report = parse_playwright_result(result, include_files=True)
        self.assertEqual((report['returncode'], report['passed']), (0, 1), report)
        self.assertEqual(report['cases'][0]['asserted_ids_by_run'], [[]])
        require_coverage(report, ['STEP-CREATE'], executed=False)
        with self.assertRaisesRegex(ValueError, 'Missing executed browser assertions.*STEP-CREATE'):
            require_coverage(report, ['STEP-CREATE'], executed=True)

    def test_frozen_browser_oracle_rejects_three_mutants(self):
        output = Path(self.enterContext(TemporaryDirectory()))
        junction = self.link_modules(output)
        self.addCleanup(os.rmdir if os.name == 'nt' else Path.unlink, junction)
        compiler = CompactCompiler(output / 'unused', output)
        with patch.object(E2EChecks, 'install_browser'):
            compiler.prepare()
        compiler.data = {'coverage': IDS}
        compiler.checks = E2EChecks(output)
        spec = output / SPEC
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(render_suite(ACCEPTANCE, IDS), encoding='utf8')
        compiler.frozen_tests = sha256(spec.read_bytes()).hexdigest()
        correct = APP.replace('/api/WRONG', '/api/items')
        app_file = output / 'frontend/src/App.tsx'
        api_file = output / 'backend/src/business/index.ts'
        app_file.write_text(correct, encoding='utf8')
        api_file.write_text(BUSINESS, encoding='utf8')
        typed = compiler.checks.typecheck([SPEC], full_source=False)
        self.assertEqual(typed['returncode'], 0, typed['output'])
        reference = compiler.verify()
        self.assertEqual(reference['passed'], 1)
        self.assertEqual(reference['scenario_mapping']['mapped_ids'], sorted(IDS))
        expected_ids = [step['id'] for step in ACCEPTANCE['cases'][0]['steps']]
        evidence_path = output / '.arc/compact-last-tests.json'
        report = json.loads(evidence_path.read_text(encoding='utf8'))
        self.assertEqual(report['cases'][0]['asserted_ids_by_run'], [expected_ids])
        print('Correct implementation passed all three original IDs.', flush=True)
        mutants = [
            ('delete-link', app_file, correct, '<button onClick={()=>setDeleting(row)}>Delete</button>',
             '<a href="#delete" onClick={()=>setDeleting(row)}>Delete</a>', 'STEP-DELETE', "Delete", 8),
            ('cancel-saves', app_file, correct, '<Link href="/">Cancel</Link>',
             '<Link href="/" onClick={e=>{e.preventDefault();e.currentTarget.closest("form")!.requestSubmit();}}>Cancel</Link>',
             'STEP-EDIT', 'Blue pen', 5),
            ('list-consumes-data', api_file, BUSINESS, "r.json(store.list('items'));",
             "const rows=store.list('items');r.json(rows);for(const row of rows)store.delete('items',row.id);",
             'STEP-CREATE', 'Blue pen', 3),
        ]
        for name, path, original, old, new, step_id, locator, passed_before_failure in mutants:
            with self.subTest(name=name):
                self.assertEqual(original.count(old), 1)
                path.write_text(original.replace(old, new), encoding='utf8')
                try:
                    with self.assertRaises(CheckFailed) as caught:
                        compiler.verify()
                    self.assertEqual(caught.exception.feedback['kind'], 'acceptance')
                    evidence = json.loads(evidence_path.read_text(encoding='utf8'))
                    self.assertEqual((evidence['passed'], evidence['failed']), (0, 1), evidence)
                    failure = evidence['failures'][0]
                    for expected in (step_id, locator, 'toBeVisible'):
                        self.assertIn(expected, failure)
                    receipts, = evidence['cases'][0]['asserted_ids_by_run']
                    self.assertEqual(receipts, expected_ids[:passed_before_failure])
                    self.assertEqual(sha256(spec.read_bytes()).hexdigest(), compiler.frozen_tests)
                    print(f'{name}: {step_id} rejected at {locator!r} after {len(receipts)} passing assertions.', flush=True)
                finally:
                    path.write_text(original, encoding='utf8')

    def test_full_compact_route_repairs_real_browser_failure_and_preserves_runtime(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); source=root/'input';source.mkdir();output=root/'application';output.mkdir()
            (source/'requirements.yaml').write_text(yaml.safe_dump(requirements()),encoding='utf8')
            junction=self.link_modules(output)
            requests=[]
            # Exercise the actual TextModel policy/serialization path, intercepting only transport.
            model=TextModel.__new__(TextModel);model.name='deepseek-flash';model.reasoning='low'
            model.max_output_tokens=320000;model.json_mode=True
            def transport(**kwargs):
                from types import SimpleNamespace
                payload=json.loads(kwargs['messages'][1]['content']);requests.append(payload)
                if payload['task'].get('test_path'):
                    self.assertNotIn('files',payload)
                    response=ACCEPTANCE
                elif payload['task'].get('phase')=='joint_application':
                    response={'files':[{'path':'backend/src/business/index.ts','content':BUSINESS},
                                       {'path':'frontend/src/App.tsx','content':APP}]}
                else:
                    self.assertEqual(payload['task']['original_requirements'],requirements())
                    self.assertFalse(payload['files'][SPEC]['writable'])
                    self.assertTrue(all(not r['writable'] for p,r in payload['files'].items() if '/runtime/' in p))
                    response={'tool_calls':[{'tool':'edit_file','path':'frontend/src/App.tsx','operation':'replace',
                        'old_content':"'/api/WRONG'",'new_content':"'/api/items'"}]}
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(
                    content=json.dumps(response),refusal=None))])
            model._request=transport
            try:
                compiler=CompactCompiler(source,output,model=model)
                with patch.object(E2EChecks,'install_browser'):
                    compiler.compile()
                self.assertEqual(compiler.report['status'],'GREEN',compiler.report)
                self.assertEqual(len(requests),3) # independent test author + full app + one real repair
                self.assertEqual(compiler.report['verification']['passed'],1)
                self.assertEqual(len(compiler.report['verification']['scenario_mapping']['mapped_ids']),3)
                self.assertFalse(compiler.report['verification']['official_evaluation_performed'])
                verify_runtime(output)
                partial=compiler.assess_partial()
                self.assertTrue(partial['production_health'])
                self.assertFalse(partial['business_acceptance_passed'])
                # Production build, real HTTP status/errors, restart durability, namespace isolation,
                # and transaction rollback. Uses fresh files, never the runner's test database.
                script=output/'runtime-check.mjs'
                script.write_text(RUNTIME_CHECK,encoding='utf8')
                result=capture_process([shutil.which('node'),str(script)],cwd=output,timeout=45)
                self.assertEqual(result['returncode'],0,result['output'])
                import importlib.util
                tool=Path(__file__).resolve().parents[1]/'team-tools/check-compact.py'
                specification=importlib.util.spec_from_file_location('compact_local_check',tool)
                checker=importlib.util.module_from_spec(specification);specification.loader.exec_module(checker)
                with patch.object(TextModel,'__init__',side_effect=AssertionError('Local checks must never construct a model')):
                    self.assertEqual(checker.main([str(output)]),0)
                    acceptance=output/SPEC;original=acceptance.read_bytes();acceptance.write_bytes(original+b'\n// tampered\n')
                    try:self.assertEqual(checker.main([str(output),'--report',str(output/'.arc/tamper-check.json')]),1)
                    finally:acceptance.write_bytes(original)
                report={'status':'GREEN','scripted_logical_requests':len(requests),'browser_journeys':1,
                        'mapped_original_steps':3,'production_runtime_checks':json.loads(result['stdout']),
                        'zero_model_local_cli':True,'tampered_acceptance_rejected':True,
                        'partial_output_production_startup_checked':True,
                        'paid_model_calls':0,'official_evaluation_performed':False}
                print(json.dumps(report,ensure_ascii=False))
                receipt=os.getenv('ARC_B6_LOCAL_REPORT')
                if receipt:Path(receipt).write_text(json.dumps(report,indent=2),encoding='utf8')
            finally:
                if os.name=='nt':os.rmdir(junction)
                else:junction.unlink()


RUNTIME_CHECK=r'''import assert from 'node:assert/strict';
import Database from 'better-sqlite3';
import {DocumentStore} from './backend/dist/runtime/store.js';
import {createApp} from './backend/dist/app.js';
import {initializeDatabase} from './backend/dist/db/initialize.js';
import {join} from 'node:path';
const file=join(process.cwd(),'persistence-check.db');
let db=new Database(file);initializeDatabase(db);let store=new DocumentStore(db);
const first=store.create('messages',{text:'hello'});
store.create('projects',{name:'general project'},first.id);
assert.equal(store.get('messages',first.id).data.text,'hello');assert.equal(store.list('projects').length,1);
assert.throws(()=>store.transaction(()=>{store.create('messages',{text:'rolled back'});throw Error('rollback');}));
assert.equal(store.list('messages').length,1);assert.throws(()=>store.list('x; DROP TABLE arc_records'));
db.close();db=new Database(file);initializeDatabase(db);store=new DocumentStore(db);
assert.equal(store.get('messages',first.id).data.text,'hello');
const server=createApp(db).listen(0,'127.0.0.1');await new Promise(r=>server.once('listening',r));
const base='http://127.0.0.1:'+server.address().port;
try{
 let r=await fetch(base+'/api/items',{method:'POST',headers:{'content-type':'application/json'},body:'{"name":""}'});
 assert.equal(r.status,400);assert.equal((await r.json()).error,'Name is required');
 r=await fetch(base+'/api/items',{method:'POST',headers:{'content-type':'application/json'},body:'{"name":"ruler"}'});
 assert.equal(r.status,201);const item=await r.json();assert.equal(item.data.name,'ruler');
 r=await fetch(base+'/api/items/'+item.id,{method:'PATCH',headers:{'content-type':'application/json'},body:'{"name":"long ruler"}'});
 assert.equal(r.status,200);assert.equal((await r.json()).data.name,'long ruler');
 r=await fetch(base+'/api/items/'+item.id,{method:'DELETE'});assert.equal(r.status,204);assert.equal(await r.text(),'');
 r=await fetch(base+'/api/items/'+item.id);assert.equal(r.status,404);
 r=await fetch(base+'/items/deep-link');assert.equal(r.status,200);assert.match(await r.text(),/<div id="root">/);
 r=await fetch(base+'/api/missing');assert.equal(r.status,404);
}finally{await new Promise(r=>server.close(r));db.close();}
console.log(JSON.stringify({sqlite_restart:true,transaction_rollback:true,resource_isolation:true,
 http_validation:true,create_update_delete:true,empty_204:true,deep_link_fallback:true,unknown_api_404:true}));
'''


if __name__=='__main__':unittest.main()
