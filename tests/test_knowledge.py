"""Knowledge freshness uses explicit source boundaries, never document mtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import knowledge


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / '.agents'
        self.root.mkdir()
        self.repo = self.workspace / 'engine'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/main.py').write_text('value = 1\n')
        (self.repo / 'src/old.py').write_text('old = True\n')
        (self.repo / 'README.md').write_text('# Engine\n')
        self.commit = self.save('initial source')
        self.config = {'workspace_root': str(self.workspace), 'projects': {'engine': {'path': 'engine'}}}
        self.path = '.share/knowledge/projects/atlas.md'
        target = self.root / self.path
        target.parent.mkdir(parents=True)
        target.write_text('# Atlas\n')
        self.event = {'event_id': 'source-event', 'created_at': '2026-10-01T00:00:00Z',
                      'task_id': 'atlas-build', 'projects': [{'name': 'engine'}]}
        self.events = [self.event]

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def save(self, message):
        self.git('add', '.')
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def metadata(self, *, covers=None, sources=None, verified='2026-10-02T00:00:00Z', wrapped=False):
        value = {'events': [{'event': self.event}] if wrapped else [self.event], 'knowledge': {
            'sources': sources if sources is not None else [
                {'repository': 'engine', 'commit': self.commit, 'covers': covers or ['src/**']},
            ],
        }}
        if verified is not None:
            value['knowledge']['verified_at'] = verified
        (self.root / self.path).with_suffix('.json').write_text(json.dumps(value))
        return value

    def check(self):
        return knowledge.document_freshness(self.root, self.path, self.events, self.config)

    def test_no_metadata_is_unknown_even_if_mtime_is_recent(self):
        os.utime(self.root / self.path, None)
        with mock.patch.object(knowledge.subprocess, 'run') as run:
            report = self.check()
        self.assertEqual(report['state'], 'unknown')
        self.assertIsNone(report['verified_at'])
        self.assertIn('修改时间', report['diagnostics'][0])
        run.assert_not_called()

    def test_existing_source_formats_preserve_ids_without_claiming_verification(self):
        for wrapped in (False, True):
            value = self.metadata(wrapped=wrapped)
            del value['knowledge']
            (self.root / self.path).with_suffix('.json').write_text(json.dumps(value))
            report = self.check()
            self.assertEqual(report['state'], 'unknown')
            self.assertEqual(report['source_event_ids'], ['source-event'])

    def test_current_reports_source_commit_scope_and_explicit_verification(self):
        self.metadata(wrapped=True)
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = self.check()
        self.assertEqual(report['state'], 'current')
        self.assertEqual(report['verified_at'], '2026-10-02T00:00:00Z')
        self.assertTrue(report['checked_at'])
        self.assertEqual(report['source_event_ids'], ['source-event'])
        self.assertEqual(report['sources'][0]['current_commit'], self.commit)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_missing_verified_time_is_not_replaced_by_mtime(self):
        self.metadata(verified=None)
        report = self.check()
        self.assertEqual(report['state'], 'current')
        self.assertIsNone(report['verified_at'])

    def test_unrelated_commit_and_dirty_file_do_not_invalidate_covered_source(self):
        self.metadata()
        (self.repo / 'README.md').write_text('# Unrelated documentation\n')
        new = self.save('documentation only')
        (self.repo / 'README.md').write_text('# Another documentation edit\n')
        report = self.check()
        self.assertEqual(report['state'], 'current')
        self.assertNotEqual(new, self.commit)
        self.assertEqual(report['sources'][0]['current_commit'], new)
        self.assertEqual(report['sources'][0]['changed_paths'], [])

    def test_covered_committed_change_is_stale(self):
        self.metadata()
        (self.repo / 'src/main.py').write_text('value = 2\n')
        self.save('covered source changed')
        report = self.check()
        self.assertEqual(report['state'], 'stale')
        self.assertEqual(report['sources'][0]['changed_paths'], ['src/main.py'])

    def test_covered_dirty_staged_and_untracked_changes_are_stale(self):
        self.metadata()
        (self.repo / 'src/main.py').write_text('value = 2\n')
        self.git('add', 'src/main.py')
        (self.repo / 'src/old.py').unlink()
        (self.repo / 'src/new.py').write_text('new = 1\n')
        report = self.check()
        self.assertEqual(report['state'], 'stale')
        self.assertEqual(report['sources'][0]['changed_paths'], ['src/main.py', 'src/new.py', 'src/old.py'])

    def test_rename_out_of_scope_includes_original_path(self):
        self.metadata(covers=['src/main.py'])
        self.git('mv', 'src/main.py', 'moved.py')
        report = self.check()
        self.assertEqual(report['state'], 'stale')
        self.assertIn('src/main.py', report['sources'][0]['changed_paths'])

    def test_globs_are_rooted_and_double_star_includes_zero_components(self):
        self.metadata(covers=['src/**/*.py'])
        (self.repo / 'other/src').mkdir(parents=True)
        (self.repo / 'other/src/ignored.py').write_text('ignored = 1\n')
        self.assertEqual(self.check()['state'], 'current')
        (self.repo / 'src/main.py').write_text('changed = 1\n')
        self.assertEqual(self.check()['state'], 'stale')
        self.metadata(covers=['src/*.py'])
        (self.repo / 'src/main.py').write_text('value = 1\n')
        (self.repo / 'src/deeper').mkdir()
        (self.repo / 'src/deeper/nested.py').write_text('nested = 1\n')
        self.assertEqual(self.check()['state'], 'current')

    def test_new_related_event_requests_review_without_declaring_source_stale(self):
        self.metadata()
        self.events.extend([
            self.event | {'event_id': 'related-new', 'created_at': '2026-10-03T00:00:00Z'},
            self.event | {'event_id': 'unrelated-new', 'task_id': 'other-work', 'created_at': '2026-10-04T00:00:00Z'},
        ])
        report = self.check()
        self.assertEqual(report['state'], 'needs_review')
        self.assertEqual(report['sources'][0]['state'], 'current')
        self.assertEqual(report['newer_event_ids'], ['related-new'])
        self.assertIn('不证明', report['diagnostics'][0])

    def test_explicit_source_correction_across_tasks_and_repositories_requests_review(self):
        self.metadata()
        for projects in ([{'name': 'engine'}], [{'name': 'other-repository'}]):
            with self.subTest(projects=projects):
                self.events = [self.event, self.event | {
                    'event_id': 'source-correction', 'created_at': '2026-10-03T00:00:00Z',
                    'task_id': 'other-work', 'projects': projects, 'supersedes': ['source-event'],
                }, self.event | {
                    'event_id': 'unrelated-correction', 'created_at': '2026-10-04T00:00:00Z',
                    'task_id': 'other-work', 'projects': projects, 'supersedes': ['unrelated-source'],
                }]
                report = self.check()
                self.assertEqual(report['state'], 'needs_review')
                self.assertEqual(report['sources'][0]['state'], 'current')
                self.assertEqual(report['source_event_ids'], ['source-event'])
                self.assertEqual(report['newer_event_ids'], ['source-correction'])

    def test_transitive_correction_follows_sources_across_verification_boundary(self):
        self.metadata()
        earlier = self.event | {
            'event_id': 'earlier-correction', 'created_at': '2026-10-01T12:00:00Z',
            'task_id': 'other-work', 'projects': [{'name': 'other-repository'}],
            'supersedes': ['source-event'],
        }
        later = earlier | {
            'event_id': 'later-correction', 'created_at': '2026-10-03T00:00:00Z',
            'supersedes': ['earlier-correction'],
        }
        self.events.extend([earlier, later])
        report = self.check()
        self.assertEqual(report['state'], 'needs_review')
        self.assertEqual(report['newer_event_ids'], ['later-correction'])

    def test_source_corrections_at_or_before_verification_do_not_request_review(self):
        self.metadata()
        self.events.extend(self.event | {
            'event_id': f'correction-{index}', 'created_at': when,
            'task_id': 'other-work', 'projects': [{'name': 'other-repository'}],
            'supersedes': ['source-event'],
        } for index, when in enumerate(('2026-10-01T12:00:00Z', '2026-10-02T00:00:00Z')))
        report = self.check()
        self.assertEqual(report['state'], 'current')
        self.assertEqual(report['newer_event_ids'], [])

    def test_source_correction_without_verification_time_uses_source_time(self):
        self.metadata(verified=None)
        self.events.append(self.event | {
            'event_id': 'source-correction', 'created_at': '2026-10-01T12:00:00Z',
            'task_id': 'other-work', 'supersedes': ['source-event'],
        })
        report = self.check()
        self.assertEqual(report['state'], 'needs_review')
        self.assertIsNone(report['verified_at'])
        self.assertEqual(report['newer_event_ids'], ['source-correction'])

    def test_source_correction_preserves_stale_and_unknown_source_states(self):
        self.metadata()
        self.events.append(self.event | {
            'event_id': 'source-correction', 'created_at': '2026-10-03T00:00:00Z',
            'task_id': 'other-work', 'supersedes': ['source-event'],
        })
        (self.repo / 'src/main.py').write_text('value = 2\n')
        report = self.check()
        self.assertEqual(report['state'], 'stale')
        self.assertEqual(report['newer_event_ids'], ['source-correction'])
        self.config['projects']['engine']['path'] = 'absent'
        report = self.check()
        self.assertEqual(report['state'], 'unknown')
        self.assertEqual(report['newer_event_ids'], ['source-correction'])

    def test_unknown_commit_and_unavailable_or_unregistered_repositories_are_unknown(self):
        for source in (
            {'repository': 'engine', 'commit': 'f' * 40, 'covers': ['src/**']},
            {'repository': 'missing', 'commit': self.commit, 'covers': ['src/**']},
        ):
            self.metadata(sources=[source])
            report = self.check()
            self.assertEqual(report['state'], 'unknown')
            self.assertTrue(report['sources'][0]['diagnostics'])
        self.metadata()
        self.config['projects']['engine']['path'] = 'absent'
        self.assertEqual(self.check()['state'], 'unknown')

    def test_repository_symlink_escape_is_rejected_before_running_git(self):
        self.metadata()
        with tempfile.TemporaryDirectory() as outside:
            (self.workspace / 'escaped').symlink_to(outside, target_is_directory=True)
            self.config['projects']['engine']['path'] = 'escaped'
            with mock.patch.object(knowledge.subprocess, 'run') as run:
                report = self.check()
            run.assert_not_called()
        self.assertEqual(report['state'], 'unknown')
        self.assertIn('越出', report['sources'][0]['diagnostics'][0])

    def test_document_and_sidecar_symlink_escapes_are_rejected(self):
        self.metadata()
        target = self.root / self.path
        sidecar = target.with_suffix('.json')
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'outside.json'
            external.write_text('{}')
            sidecar.unlink()
            sidecar.symlink_to(external)
            self.assertEqual(self.check()['state'], 'unknown')
            target.unlink()
            target.symlink_to(Path(outside) / 'outside.md')
            self.assertEqual(self.check()['state'], 'unknown')
        for path in ('../outside.md', '/tmp/outside.md'):
            self.assertEqual(knowledge.document_freshness(self.root, path, [], self.config)['state'], 'unknown')

    def test_covered_symlink_escape_and_out_of_bounds_patterns_are_unknown(self):
        self.metadata()
        (self.repo / 'src/main.py').unlink()
        (self.repo / 'src/main.py').symlink_to(self.root / self.path)
        self.assertEqual(self.check()['state'], 'unknown')
        for pattern in ('../secret', '/etc/passwd', ':(top)**', 'C:\\outside'):
            self.metadata(covers=[pattern])
            report = self.check()
            self.assertEqual(report['state'], 'unknown')
            self.assertTrue(report['diagnostics'])

    def test_empty_coverage_invalid_metadata_and_invalid_time_are_unknown(self):
        self.metadata(covers=['does-not-exist/**'])
        self.assertEqual(self.check()['state'], 'unknown')
        self.metadata(verified='2026-10-02')
        self.assertEqual(self.check()['state'], 'unknown')
        (self.root / self.path).with_suffix('.json').write_text('not json')
        self.assertEqual(self.check()['state'], 'unknown')

    def test_git_timeout_is_unknown_and_does_not_expose_raw_process_output(self):
        self.metadata()
        with mock.patch.object(knowledge.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 3)):
            report = self.check()
        self.assertEqual(report['state'], 'unknown')
        self.assertIn('超时', report['sources'][0]['diagnostics'][0])

    def test_batch_reuses_git_queries_and_limits_paths_to_selected_coverage(self):
        self.metadata()
        other = self.root / '.share/knowledge/projects/other.md'
        other.write_text('# Other\n')
        other.with_suffix('.json').write_text((self.root / self.path).with_suffix('.json').read_text())
        actual = subprocess.run
        with mock.patch.object(knowledge.subprocess, 'run', wraps=actual) as run:
            reports = knowledge.freshness_index(self.root, [{'path': self.path}, {'path': other.relative_to(self.root).as_posix()}],
                                                self.events, self.config)
        self.assertEqual({report['state'] for report in reports.values()}, {'current'})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(sum('status' in command for command in commands), 1)
        self.assertEqual(sum('diff' in command for command in commands), 1)
        self.assertEqual(sum('ls-tree' in command for command in commands), 1)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['timeout'], knowledge.GIT_TIMEOUT)
            self.assertEqual(call.kwargs['env']['GIT_NO_LAZY_FETCH'], '1')
        for command in commands:
            if 'status' in command or 'diff' in command or 'ls-tree' in command:
                self.assertEqual(command[command.index('--') + 1:], ['src'])

    def test_batch_reuses_failed_commit_queries_without_repeating_timeouts(self):
        self.metadata(sources=[{'repository': 'engine', 'commit': 'f' * 40, 'covers': ['src/**']}])
        other = self.root / '.share/knowledge/projects/other.md'
        other.write_text('# Other\n')
        other.with_suffix('.json').write_text((self.root / self.path).with_suffix('.json').read_text())
        actual = subprocess.run
        with mock.patch.object(knowledge.subprocess, 'run', wraps=actual) as run:
            reports = knowledge.freshness_index(self.root, [self.path, other.relative_to(self.root).as_posix()],
                                                self.events, self.config)
        self.assertEqual({report['state'] for report in reports.values()}, {'unknown'})
        self.assertEqual(sum('f' * 40 + '^{commit}' in call.args[0] for call in run.call_args_list), 1)

    def test_malformed_embedded_task_identity_cannot_break_other_documents(self):
        value = self.metadata()
        value['events'] = [self.event | {'event_id': 'missing-source', 'task_id': ['invalid']}]
        (self.root / self.path).with_suffix('.json').write_text(json.dumps(value))
        report = self.check()
        self.assertEqual(report['state'], 'current')
        self.assertEqual(report['source_event_ids'], ['missing-source'])


if __name__ == '__main__':
    unittest.main()
