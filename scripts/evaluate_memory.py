#!/usr/bin/env python3
"""Evaluate the real CLI against small synthetic evidence; writes only temp files."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

try:
    from scripts import retrieval
except ModuleNotFoundError:
    import retrieval

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/memory-quality.json'


def fixture_events(dataset):
    defaults = dataset.get('defaults', {})
    events = [defaults | event for event in dataset['events']]
    for i in range(dataset.get('noise_tasks', 0)):
        events.append(defaults | {'event_id':'noise-'+str(i), 'title':'历史工作记录',
                                  'content':{'what':'常规进展', 'where':['synthetic fixture']},
                                  'event_type':'progress', 'task_id':'historical-unclassified-task-'+str(i)})
    return events


def evaluate(fixture: Path = FIXTURE, *, baseline_ref: str | None = None) -> dict:
    dataset = json.loads(fixture.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='backstage-quality-') as directory:
        root = Path(directory) / '.agents'
        root.mkdir()
        config = dataset['config'] | {'workspace_root': str(root.parent), 'projects': {}}
        # Event sources are synthetic and unregistered deliberately: no host repo reads.
        (root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        daily = root / '.share/memory/fixture-machine/codex/2026-10-19.json'
        daily.parent.mkdir(parents=True)
        daily.write_text(json.dumps({'schema_version':1, 'machine_id':'fixture-machine', 'agent':'codex',
                                     'date':'2026-10-19', 'events':fixture_events(dataset)}), encoding='utf-8')
        for path, text in dataset.get('documents', {}).items():
            if not isinstance(path, str) or not isinstance(text, str) or Path(path).is_absolute():
                raise ValueError('fixture 文档必须使用相对路径和文本内容')
            try:
                target = retrieval.document_path(root, path, must_exist=False)
            except (PermissionError, OSError, RuntimeError) as exc:
                raise ValueError('fixture 文档路径必须位于临时工作区的文档目录') from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        script = ROOT / 'scripts/memory.py'
        if baseline_ref:
            source = subprocess.run(['git', 'show', baseline_ref + ':scripts/memory.py'], cwd=ROOT,
                                    check=True, capture_output=True).stdout
            script = Path(directory) / 'baseline-memory.py'
            script.write_bytes(source)
        rows = []
        for case in dataset['cases']:
            kind = case['kind']
            if kind in {'search', 'documents'}:
                arguments = ['search', case['query'], '--json', '--limit', '5']
                if kind == 'documents':
                    arguments.append('--documents')
                elif case.get('current', True):
                    arguments.append('--current')
            elif kind == 'brief':
                arguments = ['brief', '--max-chars', str(case['max_chars'])]
            else:
                arguments = ['handoff', '--json']
            for key in ('project', 'task', 'sort'):
                if key in case:
                    arguments += ['--' + key, case[key]]
            start = time.perf_counter()
            result = subprocess.run([sys.executable, str(script), '--root', str(root), *arguments], cwd=ROOT,
                                    capture_output=True, text=True, timeout=15)
            elapsed = (time.perf_counter() - start) * 1000
            ids = []
            text = result.stdout
            if kind in {'search', 'documents'} and result.returncode == 0:
                ids = [e['event_id'] if kind == 'search' else e['path'] for e in json.loads(text)]
                text = '\n'.join(ids)
            found = [value for value in case['expected'] if value in (ids if kind in {'search','documents'} else text)]
            forbidden = [value for value in case.get('forbidden', []) if value in text]
            passed = (result.returncode == 0 and len(found) == len(case['expected']) and not forbidden
                      and (kind not in {'search', 'documents'} or bool(case['expected']) or not ids)
                      and (not case.get('first') or bool(ids) and ids[0] == case['first']))
            rows.append({'name':case['name'], 'kind':kind, 'passed':passed, 'exit_code':result.returncode,
                         'found':len(found), 'expected':len(case['expected']), 'forbidden_hits':forbidden,
                         'returned':len(ids) if kind in {'search', 'documents'} else None,
                         'chars':len(result.stdout), 'cli_ms':round(elapsed, 2)})
    search = [r for r in rows if r['kind'] == 'search' and r['expected']]
    return {'baseline_ref':baseline_ref, 'cases':len(rows), 'passed':sum(r['passed'] for r in rows),
            'by_kind':dict(Counter(r['kind'] for r in rows)),
            'hit_at_5':sum(r['found'] > 0 for r in search)/len(search) if search else None,
            'evidence_recall_at_5':sum(r['found'] for r in search)/sum(r['expected'] for r in search) if search else None,
            'cli_p50_ms':round(statistics.median(r['cli_ms'] for r in rows), 2) if rows else None,
            'notice':'固定合成样例，仅衡量这些用例；CLI耗时含进程启动。旧版本不支持的命令计失败，按kind查看差异。',
            'results':rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', type=Path, default=FIXTURE)
    parser.add_argument('--baseline-ref', help='从本地 Git ref 读取旧 CLI；不切换分支、不联网')
    args = parser.parse_args()
    try:
        report = evaluate(args.fixture, baseline_ref=args.baseline_ref)
    except (OSError, ValueError) as exc:
        print(f'memory-eval: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] == report['cases'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
