"""Synthetic quality, privacy-boundary and read-only regressions (stdlib only)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import doctor, evaluate_memory, memory, retrieval, sync


class MemoryQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / '.agents'
        self.root.mkdir()
        self.dataset = json.loads(evaluate_memory.FIXTURE.read_text())
        self.events = [memory.normalize_event(e) for e in evaluate_memory.fixture_events(self.dataset)]
        self.config = self.dataset['config'] | {'workspace_root': self.temp.name, 'projects':{}}
        (self.root / 'config.json').write_text(json.dumps(self.config))
        memory.append_events(self.root, self.events)

    def tearDown(self):
        self.temp.cleanup()

    def brief(self, *args):
        options = memory.parser().parse_args(['brief', '--project', 'atlas-core', *args])
        return memory.build_brief(self.events, options)

    def files(self):
        return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def cli(self, *args):
        return subprocess.run([sys.executable, str(Path(memory.__file__)), '--root', str(self.root), *args],
                              capture_output=True, text=True, encoding='utf-8')

    def fixture(self, **changes):
        path = Path(self.temp.name) / 'quality.json'
        path.write_text(json.dumps(self.dataset | changes), encoding='utf-8')
        return path

    def test_brief_task_volume_cannot_bury_facts_risks_or_sources(self):
        text = memory.render_brief(self.brief(), 6000)
        self.assertLessEqual(len(text), 6000)
        for label in ('约定与决策', '事实与验证记录', '当前进展', '风险与待验证'):
            self.assertIn(label, text)
        for source in ('interface-new', 'validation', 'risk'):
            self.assertIn('来源: '+source, text)
        self.assertIn('其余', text)
        self.assertNotIn('interface-old', text)

    def test_small_budgets_never_slice_an_event_identifier(self):
        report = self.brief('--task', 'atlas-delivery')
        full = 'evt_' + 'a' * 200
        for rows in report['sections'].values():
            for row in rows:
                row['sources'] = [full]
        for budget in (1, 80, 200, 450, 700, 2400, *range(1, 1500, 17)):
            text = memory.render_brief(report, budget)
            self.assertLessEqual(len(text), budget)
            if 'evt_' in text:
                self.assertIn(full, text)
                self.assertEqual(text.count('evt_'), text.count(full))

    def test_brief_text_preserves_lifecycle_heads_reasons_and_record_status(self):
        text = memory.render_brief(self.brief('--task', 'conflicted-task'), 6000)
        for event in self.events:
            if event['event_id'] in {'conflict-a', 'conflict-b'}:
                self.assertIn('[' + event['event_id'] + ']', text)
                self.assertIn(event['content']['what'], text)
                self.assertIn(event['content']['where'][0], text)
        self.assertIn('(progress/active)', text)
        result = self.cli('brief', '--task', 'atlas-delivery', '--max-chars', '1200')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLessEqual(len(result.stdout), 1200)  # Includes CLI's trailing newline.

    def test_handoff_preserves_failures_evidence_and_explicit_status_without_writes(self):
        before = self.files()
        sources = {}
        events, errors = memory.load_events(self.root, sources=sources)
        self.assertFalse(errors)
        for _ in range(2):
            result = memory.handoff(self.root, events, 'atlas-delivery', sources=sources)
            self.assertEqual(result['state'], 'active')
            self.assertEqual(result['sections']['失败与修复记录'][0]['event_id'] in {'risk','failed'}, True)
            validation = result['sections']['验证结果'][0]
            self.assertEqual(validation['event_id'], 'validation')
            self.assertTrue(validation['source'].startswith('.share/memory/'))
            self.assertTrue(result['sections']['后续候选'])
        self.assertEqual(before, self.files())

    def test_closed_and_conflicting_tasks_keep_experience_without_next_steps(self):
        for task, state in [('archived-task','closed'), ('conflicted-task','conflict')]:
            result = memory.handoff(self.root, self.events, task)
            self.assertEqual(result['state'], state)
            self.assertFalse(result['sections']['后续候选'])
        closed = memory.handoff(self.root, self.events, 'archived-task')
        self.assertIn('缓冲区不足', closed['sections']['事实与依据'][0]['text'])
        with self.assertRaises(FileNotFoundError):
            memory.handoff(self.root, self.events, 'missing')

    def test_cli_handoff_missing_and_doctor_return_structured_results(self):
        script = Path(memory.__file__)
        result = subprocess.run([sys.executable, str(script), '--root', str(self.root), 'handoff', '--task', 'missing'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)
        result = subprocess.run([sys.executable, str(script), '--root', str(self.root), 'doctor', '--json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)  # Intentionally absent shared links.
        self.assertEqual(json.loads(result.stdout)['scope'], 'local')

    def test_handoff_text_exposes_state_evidence_omissions_and_repo_availability(self):
        result = self.cli('handoff', '--task', 'conflicted-task')
        self.assertEqual(result.returncode, 0, result.stderr)
        for event in self.events:
            if event['event_id'] in {'conflict-a', 'conflict-b'}:
                self.assertIn(event['event_id'], result.stdout)
                self.assertIn(event['content']['what'], result.stdout)
        result = self.cli('handoff', '--task', 'atlas-delivery', '--limit', '1')
        self.assertIn('另 ', result.stdout)
        self.assertIn('本机未注册仓库:', result.stdout)
        self.assertIn('(test_result/active)', result.stdout)
        repo = Path(self.temp.name) / 'repo'
        repo.mkdir()
        (self.root / 'config.json').write_text(json.dumps(self.config | {'projects':{'atlas-core':{'path':'repo'}}}))
        output = io.StringIO()
        options = memory.parser().parse_args(['handoff', '--task', 'atlas-delivery'])
        with mock.patch.object(subprocess, 'run', side_effect=FileNotFoundError('git')), contextlib.redirect_stdout(output):
            self.assertEqual(memory.command_handoff(self.root, options), 0)
        for value in ('提交不可用', '改动状态: 不可用'):
            self.assertIn(value, output.getvalue())
        self.assertNotIn('任务不存在', output.getvalue())

    def test_aliases_use_and_between_words_and_preserve_chinese_substring_matching(self):
        groups = retrieval.query_groups('STARBOARD 缓存', self.config['search_aliases'])
        self.assertTrue(retrieval.matches('ＡＴＬＡＳ 的缓存', groups))
        self.assertFalse(retrieval.matches('atlas other topic', groups))
        self.assertTrue(retrieval.matches('训练评测失败', retrieval.query_groups('评测')))
        for invalid in ([], {'a':'b'}, {'a':[None]}, {'':['b']}):
            with self.assertRaises(ValueError):
                retrieval.query_groups('', invalid)

    def test_doc_search_confines_sources_and_matches_body_with_snippet(self):
        docs = self.root / '.share/knowledge'
        docs.mkdir(parents=True)
        (docs / 'entry.md').write_text('# Plain title\n\nAtlas 中文缓冲区在正文中。')
        (docs / 'escape.md').symlink_to(Path(self.temp.name) / 'private.md')
        (Path(self.temp.name) / 'private.md').write_text('private secret text')
        result = retrieval.search_documents(retrieval.scan_documents(self.root), 'starboard 缓冲区', aliases=self.config['search_aliases'])
        self.assertEqual([d['path'] for d in result], ['.share/knowledge/entry.md'])
        self.assertIn('中文缓冲区', result[0]['excerpt'])
        self.assertNotIn('text', result[0])
        with self.assertRaises(PermissionError):
            retrieval.document_path(self.root, '.share/knowledge/escape.md')

    def test_unicode_snippet_positions_follow_normalized_display_text(self):
        for prefix in ('ﬁ' * 100, 'ß' * 200, 'Ａ' * 200, 'e\u0301' * 200):
            result = retrieval.excerpt(prefix + ' target 中文证据', retrieval.query_groups('target'))
            self.assertIn('target 中文证据', result)
            self.assertLessEqual(len(result), 182)
        self.assertIn('Straße', retrieval.excerpt('x' * 300 + ' Straße', retrieval.query_groups('STRASSE')))

    def test_document_search_does_not_require_valid_memory_or_config_and_accepts_empty_query(self):
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'entry.md').write_text('# Searchable\n\nneedle in body')
        memory.memory_files(self.root)[0].write_text('{broken')
        (self.root / 'config.json').unlink()
        for query in ('', '   ', 'needle'):
            result = self.cli('search', query, '--documents')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('docs/entry.md', result.stdout)
        result = self.cli('search', 'needle', '--documents', '--json')
        self.assertEqual(json.loads(result.stdout)[0]['excerpt'], 'needle in body')
        self.assertEqual(self.cli('recent').returncode, 2)  # Raw corruption still fails event reads.

    def test_raw_search_and_recent_work_without_config(self):
        (self.root / 'config.json').unlink()
        for args in (('search', '缓冲区', '--json'), ('recent', '--json', '--limit', '1')):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout))

    def test_corrupt_optional_config_does_not_block_raw_or_document_recovery_reads(self):
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'entry.md').write_text('# Note\nneedle evidence')
        for config in ('{sensitive-broken-value', '[]', '{"search_aliases": []}'):
            (self.root / 'config.json').write_text(config)
            for args in (('recent', '--json', '--limit', '1'), ('search', '缓冲区', '--json'),
                         ('search', 'needle', '--documents', '--json')):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout))
                if args[0] == 'search':
                    self.assertIn('未应用别名', result.stderr)
                self.assertNotIn('sensitive-broken-value', result.stderr)
        # A raw copy may carry only its optional alias map, without a complete workspace config.
        (self.root / 'config.json').write_text(json.dumps({'search_aliases':self.config['search_aliases']}))
        result = self.cli('search', 'starboard', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))
        self.assertFalse(result.stderr)

    def test_unreadable_optional_config_warns_without_exposing_values(self):
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'entry.md').write_text('# Note\nneedle evidence')
        read_json = memory.read_json
        def read(path):
            if path == self.root / 'config.json':
                raise PermissionError('sensitive-config-value')
            return read_json(path)
        out, err = io.StringIO(), io.StringIO()
        args = memory.parser().parse_args(['search', 'needle', '--documents', '--json'])
        with mock.patch.object(memory, 'read_json', side_effect=read), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(memory.command_list(self.root, args), 0)
        self.assertTrue(json.loads(out.getvalue()))
        self.assertIn('未应用别名', err.getvalue())
        self.assertNotIn('sensitive-config-value', err.getvalue())

    def test_broken_config_symlink_warns_while_missing_optional_config_is_silent(self):
        docs = self.root / 'docs'
        docs.mkdir()
        (docs / 'entry.md').write_text('# Note\nneedle evidence')
        config = self.root / 'config.json'
        config.unlink()
        config.symlink_to(Path(self.temp.name) / 'missing-config.json')
        result = self.cli('search', 'needle', '--documents', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))
        self.assertIn('未应用别名', result.stderr)
        config.unlink()
        result = self.cli('search', 'needle', '--documents', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))
        self.assertFalse(result.stderr)

    def test_symlinked_document_root_does_not_grant_external_access(self):
        external = Path(self.temp.name) / 'external'
        external.mkdir()
        (external / 'outside.md').write_text('# External')
        (self.root / 'tools').symlink_to(external, target_is_directory=True)
        self.assertEqual(retrieval.scan_documents(self.root), [])
        with self.assertRaises(PermissionError):
            retrieval.document_path(self.root, 'tools/outside.md')

    def test_doctor_uses_shared_link_plan_and_does_not_write_or_contact_remote(self):
        shared = self.root / '.share/agent'
        (shared / 'skills/common/sample').mkdir(parents=True)
        (shared / 'skills/common/sample/SKILL.md').write_text('sample')
        (shared / 'prompts').mkdir()
        for name in ('AGENTS.md','CLAUDE.md'):
            (shared / 'prompts' / name).write_text('same prompt')
        home = Path(self.temp.name) / 'home'
        for path, target in sync.agent_links(self.root, self.config, home=home):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=target.is_dir())
        before = self.files()
        directories = {str(p) for p in self.root.rglob("*") if p.is_dir()}
        with mock.patch.object(subprocess, 'run', side_effect=AssertionError('No commands or network')):
            result = doctor.inspect_workspace(self.root, home=home)
        self.assertEqual(next(c['state'] for c in result['checks'] if c['name']=='Skill 与入口链接'), 'ok')
        self.assertEqual(next(c['state'] for c in result['checks'] if c['name']=='同步'), 'unconfigured')
        self.assertEqual(before, self.files())
        self.assertEqual(directories, {str(p) for p in self.root.rglob('*') if p.is_dir()})

    def test_doctor_does_not_echo_malformed_configuration(self):
        (self.root / 'config.json').write_text('{"sensitive-value":broken}')
        result = doctor.inspect_workspace(self.root)
        self.assertEqual(result['state'], 'error')
        self.assertNotIn('sensitive-value', json.dumps(result))

    def test_doctor_reports_sync_conflict_and_last_event_without_claiming_online(self):
        self.config['sync'] = {'enabled':True, 'initialized':True, 'remote':'alias/bucket/workspace'}
        (self.root / 'config.json').write_text(json.dumps(self.config))
        (self.root / '.local/.sync/conflict').mkdir(parents=True)
        result = doctor.inspect_workspace(self.root, home=Path(self.temp.name) / 'home')
        check = next(c for c in result['checks'] if c['name']=='同步')
        self.assertEqual(check['state'], 'warning')
        self.assertIn('冲突', check['detail'])
        self.assertTrue(result['last_events'])
        self.assertIn('不代表机器在线', result['notice'])

    def test_doctor_reports_symlink_loops_instead_of_crashing(self):
        target = self.root / '.share/agent/skills/common/sample'
        target.mkdir(parents=True)
        (target / 'SKILL.md').write_text('sample')
        home = Path(self.temp.name) / 'home'
        path, _ = next((p, t) for p, t in sync.agent_links(self.root, self.config, home=home)
                       if p.name == 'sample')
        path.parent.mkdir(parents=True)
        path.symlink_to(path)
        before = path.readlink()
        result = doctor.inspect_workspace(self.root, home=home)
        link_check = next(c for c in result['checks'] if c['name']=='Skill 与入口链接')
        self.assertEqual(link_check['state'], 'warning')
        self.assertIn('来源不符或断链 1', link_check['detail'])
        self.assertEqual(path.readlink(), before)

    def test_doctor_flags_null_identity_and_missing_workspace(self):
        for updates in ({'machine_id': None}, {'workspace_root':'/does/not/exist'}, {'workspace': []}):
            (self.root / 'config.json').write_text(json.dumps(self.config | updates))
            report = doctor.inspect_workspace(self.root)
            self.assertEqual(report['state'], 'error')
            self.assertEqual(report['checks'][0]['state'], 'error')

    def test_shared_config_validation_rejects_blank_root_and_invalid_project_paths(self):
        invalid = [
            {'workspace_root':''}, {'workspace_root':'   '}, {'workspace_root':None},
            {'projects':[]}, {'projects':{'atlas':'not-an-object'}}, {'projects':{'atlas':{}}},
            *({'projects':{'atlas':{'path':path}}} for path in (None, '', '   ', [], 123)),
            *({'search_aliases':aliases} for aliases in ([], {'a':'b'}, {'a':['']}, {'':['b']})),
        ]
        for updates in invalid:
            with self.subTest(updates=updates):
                (self.root / 'config.json').write_text(json.dumps(self.config | updates))
                with self.assertRaises(ValueError):
                    memory.load_config(self.root)
                report = doctor.inspect_workspace(self.root, home=Path(self.temp.name) / 'home')
                self.assertEqual(report['state'], 'error')
                self.assertEqual(report['checks'][0]['state'], 'error')

    def test_windows_patch_doctor_accepts_only_matching_regular_prompt_copies(self):
        # Execute the actual out-of-tree doctor patch on Linux; this is not a Windows runtime test.
        patched = Path(self.temp.name) / 'patched'
        (patched / 'scripts').mkdir(parents=True)
        (patched / 'scripts/doctor.py').write_bytes(Path(doctor.__file__).read_bytes())
        subprocess.run(['git', 'apply', '--include=scripts/doctor.py',
                        str(Path(memory.__file__).parents[1] / 'patches/windows-portability.patch')],
                       cwd=patched, check=True, capture_output=True)
        spec = importlib.util.spec_from_file_location('patched_doctor', patched / 'scripts/doctor.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = Path(self.temp.name) / 'shared-prompt.md'
        copy = Path(self.temp.name) / 'installed-prompt.md'
        source.write_text('same prompt')
        copy.write_bytes(source.read_bytes())
        def check(platform):
            module.os = SimpleNamespace(name=platform)
            with mock.patch.object(module.sync, 'agent_links', return_value=[(copy, source)]):
                report = module.inspect_workspace(self.root, home=Path(self.temp.name) / 'home')
            return next(c['state'] for c in report['checks'] if c['name']=='Skill 与入口链接')
        self.assertEqual(check('posix'), 'warning')
        self.assertEqual(check('nt'), 'ok')
        copy.write_text('outdated prompt')
        self.assertEqual(check('nt'), 'warning')
        copy.unlink()
        other = Path(self.temp.name) / 'other-prompt.md'
        other.write_bytes(source.read_bytes())
        copy.symlink_to(other)
        self.assertEqual(check('nt'), 'warning')  # Unrelated symlinks still have the wrong source.

    def test_git_snapshot_does_not_report_failed_status_as_clean(self):
        repo = Path(self.temp.name) / 'repo'
        repo.mkdir()
        config = self.config | {'projects':{'repo':{'path':'repo'}}}
        results = [subprocess.CompletedProcess([],0,'main'), subprocess.CompletedProcess([],0,'abc'), subprocess.CompletedProcess([],128,'')]
        with mock.patch.object(subprocess, 'run', side_effect=results):
            result = memory.project_metadata(config, ['repo'], timeout=3)
        self.assertIsNone(result[0]['dirty'])
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('git',3)):
            self.assertIsNone(memory.git_value(repo, 'status', timeout=3))
        for error in (FileNotFoundError('git'), PermissionError('git')):
            with mock.patch.object(subprocess, 'run', side_effect=error):
                self.assertIsNone(memory.git_value(repo, 'status', timeout=3))

    def test_evaluator_rejects_absolute_parent_and_non_document_writes(self):
        outside = Path(self.temp.name) / 'outside.md'
        outside.write_text('preserve')
        for path in (str(outside), '../../' + outside.name, 'docs/../../../' + outside.name, 'config.json'):
            fixture = self.fixture(documents={path:'overwritten'}, cases=[
                {'name':'path-guard','kind':'search','query':'absent-needle','expected':[]},
            ])
            with self.assertRaises(ValueError):
                evaluate_memory.evaluate(fixture)
            self.assertEqual(outside.read_text(), 'preserve')
        result = subprocess.run([sys.executable, str(Path(evaluate_memory.__file__)), '--fixture', str(fixture)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('Traceback', result.stderr)

    def test_evaluator_empty_expected_requires_empty_search_and_document_results(self):
        fixture = self.fixture(cases=[
            {'name':'false-negative-search', 'kind':'search', 'query':'缓冲区', 'expected':[]},
            {'name':'false-negative-documents', 'kind':'documents', 'query':'needle', 'expected':[]},
            {'name':'true-negative', 'kind':'search', 'query':'absent-token-12345', 'expected':[]},
        ], documents={'docs/note.md':'# Note\nneedle'})
        result = evaluate_memory.evaluate(fixture)
        self.assertEqual(result['passed'], 1)
        self.assertEqual([r['passed'] for r in result['results']], [False, False, True])
        self.assertTrue(all(r['returned'] > 0 for r in result['results'][:2]))
        self.assertIsNone(result['hit_at_5'])
        self.assertIsNone(result['evidence_recall_at_5'])

    def test_thirty_quality_cases_run_against_real_cli(self):
        result = evaluate_memory.evaluate()
        self.assertEqual(result['cases'], 30)
        self.assertEqual(result['passed'], 30, [r for r in result['results'] if not r['passed']])
        self.assertEqual(result['evidence_recall_at_5'], 1.0)

    def test_baseline_head_restores_its_own_cli_dependencies(self):
        external = Path(self.temp.name) / 'foreign'
        (external / 'scripts').mkdir(parents=True)
        (external / 'scripts/__init__.py').write_text('')
        (external / 'scripts/retrieval.py').write_text('raise RuntimeError("foreign module imported")')
        for baseline in (None, 'HEAD'):
            with self.subTest(baseline=baseline), mock.patch.dict('os.environ', {'PYTHONPATH':str(external)}):
                result = evaluate_memory.evaluate(baseline_ref=baseline)
            self.assertEqual(result['cases'], 30)
            self.assertEqual(result['passed'], 30, [r for r in result['results'] if not r['passed']])

    def test_evaluator_bad_ref_and_malformed_fixture_have_clean_error_exit(self):
        script = str(Path(evaluate_memory.__file__))
        for ref in ('not-a-real-ref-pr2', '', '   '):
            result = subprocess.run([sys.executable, script, '--baseline-ref=' + ref], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('基线版本', result.stderr)
            self.assertFalse(result.stdout)
            self.assertNotIn('Traceback', result.stderr)
        for data in ([], {}, self.dataset | {'documents':[]}, self.dataset | {'cases':[{}]},
                     self.dataset | {'cases':[{'name':'missing-query','kind':'search','expected':[]}]},
                     self.dataset | {'cases':[{'name':'bad-kind','kind':'typo','expected':[]}]}):
            fixture = Path(self.temp.name) / 'invalid-fixture.json'
            fixture.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, script, '--fixture', str(fixture)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('memory-eval:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_evaluator_cli_timeout_is_an_execution_error_without_a_quality_score(self):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', ['evaluate_memory.py']), \
                mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('synthetic CLI', 15)), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            self.assertEqual(evaluate_memory.main(), 2)
        self.assertIn('执行超时', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())
        self.assertFalse(out.getvalue())

    def test_evaluator_rejects_invalid_case_arguments_and_data_before_scoring(self):
        search = {'name':'query','kind':'search','query':'absent-needle','expected':[]}
        invalid_cases = [
            [], [{'name':'budget','kind':'brief','project':'atlas-core','max_chars':'oops','expected':[]}],
            [{'name':'budget','kind':'brief','project':'atlas-core','max_chars':True,'expected':[]}],
            [{'name':'budget','kind':'brief','project':'atlas-core','max_chars':0,'expected':[]}],
            [{'name':'handover','kind':'handoff','expected':[]}],
            [{'name':'handover','kind':'handoff','task':'','expected':[]}],
            [search | {'sort':'invalid'}], [search | {'query':123}],
            [search | {'forbidden':'needle'}], [search | {'first':False}],
            [search | {'current':'false'}], [search | {'project':[]}],
            [search | {'kind':'documents','project':'atlas'}],
            [search | {'name':''}],
        ]
        datasets = [self.dataset | {'cases':cases} for cases in invalid_cases]
        datasets += [self.dataset | {'config':{'workspace':[],'machine_id':'fixture'}},
                     self.dataset | {'events':[{'event_id':'broken'}], 'noise_tasks':0},
                     *(self.dataset | {'noise_tasks':value} for value in (-1, True, 1.5))]
        for data in datasets:
            fixture = Path(self.temp.name) / 'invalid-arguments.json'
            fixture.write_text(json.dumps(data))
            result = subprocess.run([sys.executable, str(Path(evaluate_memory.__file__)), '--fixture', str(fixture)],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(result.stdout)
            self.assertIn('memory-eval:', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_evaluator_accepts_valid_empty_queries_and_preserves_quality_failure_exit(self):
        cases = [
            {'name':'all-events','kind':'search','query':'','expected':['interface-old']},
            {'name':'all-documents','kind':'documents','query':'','expected':['docs/note.md']},
            {'name':'literal-option','kind':'search','query':'--help','expected':[]},
        ]
        fixture = self.fixture(cases=cases, events=self.dataset['events'][:1], noise_tasks=0,
                               documents={'docs/note.md':'# Note'})
        report = evaluate_memory.evaluate(fixture)
        self.assertEqual(report['passed'], 3)
        fixture = self.fixture(cases=[{'name':'valid-miss','kind':'search','query':'absent-needle','expected':['interface-old']}])
        result = subprocess.run([sys.executable, str(Path(evaluate_memory.__file__)), '--fixture', str(fixture)],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['passed'], 0)
        self.assertFalse(result.stderr)

    def test_evaluator_brief_requires_a_selector_and_accepts_either_valid_selector(self):
        case = {'name':'brief','kind':'brief','max_chars':100,'expected':[]}
        fixture = self.fixture(cases=[case])
        result = subprocess.run([sys.executable, str(Path(evaluate_memory.__file__)), '--fixture', str(fixture)],
                                capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2)
        self.assertFalse(result.stdout)
        self.assertIn('project 或 task', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        fixture = self.fixture(cases=[case | {'project':'atlas-core'}, case | {'task':'atlas-delivery'}])
        report = evaluate_memory.evaluate(fixture)
        self.assertEqual(report['cases'], 2)
        self.assertEqual(report['passed'], 2)


if __name__ == '__main__':
    unittest.main()
