#!/usr/bin/env python3
"""Evaluate the real CLI against small synthetic evidence; writes only temp files."""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile

try:
    from scripts import memory, retrieval
except ModuleNotFoundError:
    import memory
    import retrieval

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/memory-quality.json'


def fixture_events(dataset):
    defaults = dataset.get('defaults', {})
    noise_tasks = dataset.get('noise_tasks', 0)
    if type(noise_tasks) is not int or noise_tasks < 0:
        raise ValueError('fixture noise_tasks 必须是非负整数')
    events = [defaults | event for event in dataset['events']]
    for i in range(noise_tasks):
        events.append(defaults | {'event_id':'noise-'+str(i), 'title':'历史工作记录',
                                  'content':{'what':'常规进展', 'where':['synthetic fixture']},
                                  'event_type':'progress', 'task_id':'historical-unclassified-task-'+str(i)})
    return events


def evaluate(fixture: Path = FIXTURE, *, baseline_ref: str | None = None) -> dict:
    if baseline_ref is not None and (not isinstance(baseline_ref, str) or not baseline_ref.strip()):
        raise ValueError('基线版本必须是非空本地 Git 引用')
    dataset = json.loads(fixture.read_text(encoding='utf-8'))
    if (not isinstance(dataset, dict) or not isinstance(dataset.get('config'), dict)
            or not isinstance(dataset.get('events'), list) or not isinstance(dataset.get('cases'), list) or not dataset['cases']
            or not isinstance(dataset.get('documents', {}), dict)):
        raise ValueError('fixture 必须包含 config 对象、events 数组、非空 cases 数组；documents 必须是对象')
    with tempfile.TemporaryDirectory(prefix='backstage-quality-') as directory:
        root = Path(directory) / '.agents'
        root.mkdir()
        config = dataset['config'] | {'workspace_root': str(root.parent), 'projects': {}}
        # Event sources are synthetic and unregistered deliberately: no host repo reads.
        (root / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        memory.load_config(root)
        memory.append_events(root, fixture_events(dataset))
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
        cwd = ROOT
        environment = os.environ.copy()
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        if baseline_ref:
            revision = subprocess.run(['git', 'rev-parse', '--verify', '--end-of-options', baseline_ref + '^{commit}'],
                                      cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
            archive = subprocess.run(['git', 'archive', '--format=zip', revision, 'scripts'],
                                     cwd=ROOT, check=True, capture_output=True).stdout
            cwd = Path(directory) / 'baseline'
            with zipfile.ZipFile(io.BytesIO(archive)) as snapshot:
                snapshot.extractall(cwd)
            script = cwd / 'scripts/memory.py'
            if not script.is_file():
                raise ValueError('基线版本缺少 scripts/memory.py')
        # Resolve both sides from their own source tree, ignoring external module paths.
        environment['PYTHONPATH'] = str(cwd)
        commands = []
        for case in dataset['cases']:
            if (not isinstance(case, dict) or not isinstance(case.get('kind'), str)
                    or case['kind'] not in {'search', 'documents', 'brief', 'handoff'}
                    or not isinstance(case.get('expected'), list)
                    or any(not isinstance(value, str) for value in case['expected'])):
                raise ValueError('fixture 用例必须提供有效 kind 和字符串数组 expected')
            kind = case['kind']
            if not isinstance(case.get('name'), str) or not case['name'].strip():
                raise ValueError('fixture 用例 name 必须是非空字符串')
            if not isinstance(case.get('forbidden', []), list) or any(not isinstance(v, str) for v in case.get('forbidden', [])):
                raise ValueError('fixture forbidden 必须是字符串数组')
            if 'first' in case and (kind not in {'search', 'documents'} or not isinstance(case['first'], str) or not case['first']):
                raise ValueError('fixture first 仅用于检索用例，必须是非空字符串')
            if 'current' in case and (kind != 'search' or not isinstance(case['current'], bool)):
                raise ValueError('fixture current 仅用于记忆检索，必须是布尔值')
            for key in ('project', 'task', 'sort'):
                if key in case and (not isinstance(case[key], str) or not case[key].strip()):
                    raise ValueError('fixture project/task/sort 必须是非空字符串')
            if kind == 'documents' and any(key in case for key in ('project', 'task', 'current')):
                raise ValueError('fixture 文档检索不接受事件筛选')
            if kind in {'search', 'documents'}:
                if not isinstance(case.get('query'), str):
                    raise ValueError('fixture 检索 query 必须是字符串，允许空查询')
                arguments = ['search', '--json', '--limit', '5']
                if kind == 'documents':
                    arguments.append('--documents')
                elif case.get('current', True):
                    arguments.append('--current')
            elif kind == 'brief':
                if type(case.get('max_chars')) is not int or case['max_chars'] <= 0:
                    raise ValueError('fixture brief max_chars 必须是正整数')
                arguments = ['brief', '--max-chars', str(case['max_chars'])]
            else:
                arguments = ['handoff', '--json']
            for key in ('project', 'task', 'sort'):
                if key in case:
                    arguments.append('--' + key + '=' + case[key])
            if kind in {'search', 'documents'}:
                arguments += ['--', case['query']]
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    memory.parser().parse_args(arguments)
            except (SystemExit, TypeError) as exc:
                raise ValueError('fixture 用例包含无效 CLI 参数') from exc
            commands.append((case, arguments))
        rows = []
        for case, arguments in commands:
            kind = case['kind']
            start = time.perf_counter()
            result = subprocess.run([sys.executable, str(script), '--root', str(root), *arguments], cwd=cwd,
                                    capture_output=True, text=True, encoding='utf-8', timeout=15, env=environment)
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
    except subprocess.TimeoutExpired:
        print('memory-eval: CLI 执行超时（15 秒）', file=sys.stderr)
        return 2
    except subprocess.CalledProcessError:
        print('memory-eval: 无法读取基线版本；请检查本地 Git 引用和 scripts 目录', file=sys.stderr)
        return 2
    except (KeyError, TypeError):
        print('memory-eval: fixture 缺少必要字段或字段类型无效', file=sys.stderr)
        return 2
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f'memory-eval: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] == report['cases'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
