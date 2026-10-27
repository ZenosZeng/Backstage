"""Read-only knowledge provenance and scoped Git freshness checks.

A document's same-name JSON may retain ``events: [event | {event: event}]``
and add ``knowledge: {verified_at?: ISO-with-zone, sources: [{repository,
commit, covers: [relative-path-or-glob]}]}``. Repository names must be registered
in config.projects. A literal path covers itself and its descendants; glob ``*``
matches one component, ``**`` any number. Only covered changes imply ``stale``.
``current`` describes source-code agreement, not a guarantee of factual accuracy.
No metadata, unresolvable provenance or unavailable repositories mean ``unknown``.
Newer related events suggest ``needs_review``; file mtimes are never evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from scripts import retrieval


GIT_TIMEOUT = 3


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('覆盖范围必须是仓库内的相对路径或 glob')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or ':' in value or path.as_posix() == '.':
        raise ValueError('覆盖范围不得越界或使用 Git pathspec 指令')
    return path.as_posix()


def _matches(path, patterns):
    parts = path.split('/')
    for pattern in patterns:
        if not any(char in pattern for char in '*?['):
            if path == pattern or path.startswith(pattern + '/'):
                return True
        else:
            positions = {0}
            for component in pattern.split('/'):
                if not positions:
                    break
                positions = (set(range(min(positions), len(parts) + 1)) if component == '**' else
                             {index + 1 for index in positions if index < len(parts)
                              and fnmatch.fnmatchcase(parts[index], component)})
            if len(parts) in positions:
                return True
    return False


def _prefix(pattern):
    parts = []
    for part in pattern.split('/'):
        if any(char in part for char in '*?['):
            break
        parts.append(part)
    return '/'.join(parts) or '.'


def _repository(root, config, name):
    registry = config.get('projects', {})
    item = registry.get(name) if isinstance(registry, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get('path'), str) or not item['path'].strip():
        raise ValueError('来源仓库未登记，或未配置本机路径')
    workspace = Path(os.path.expandvars(os.path.expanduser(str(config.get('workspace_root', root.parent)))))
    if not workspace.is_absolute():
        workspace = root / workspace
    workspace = workspace.resolve()
    hint = Path(os.path.expandvars(os.path.expanduser(item['path'])))
    candidate = hint if hint.is_absolute() else workspace / hint
    # An absolute registration explicitly selects its parent, while relative
    # registrations cannot grant access through a symlink outside the workspace.
    boundary = candidate.parent.resolve() if hint.is_absolute() else workspace
    resolved = candidate.resolve()
    if not resolved.is_relative_to(boundary):
        raise ValueError('来源仓库路径通过符号链接或父目录越出登记范围')
    if not resolved.is_dir():
        raise ValueError('来源仓库在本机不可用')
    return resolved


class _Git:
    def __init__(self, path, covers):
        self.path = path
        self.prefixes = sorted({_prefix(pattern) for pattern in covers})
        self.cache = {}
        self.head = self.run('rev-parse', '--verify', 'HEAD').strip()
        top = Path(self.run('rev-parse', '--show-toplevel').strip()).resolve()
        if top != self.path:
            raise ValueError('登记路径不是来源 Git 仓库根目录')
        raw = self.run('status', '--porcelain=v1', '-z', '--untracked-files=all', '--', *self.prefixes)
        self.dirty = set()
        records = iter(raw.split('\x00'))
        for record in records:
            if not record:
                continue
            self.dirty.add(record[3:])
            if 'R' in record[:2] or 'C' in record[:2]:
                original = next(records, '')
                if original:
                    self.dirty.add(original)

    def run(self, *args):
        if args not in self.cache:
            env = os.environ | {'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0'}
            # Environment overrides must not redirect a confined read to another repository.
            for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                        'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'):
                env.pop(key, None)
            try:
                result = subprocess.run(
                    ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(self.path), *args],
                    capture_output=True, text=True, encoding='utf-8', errors='replace',
                    timeout=GIT_TIMEOUT, env=env, check=False,
                )
            except subprocess.TimeoutExpired as error:
                failure = ValueError('来源 Git 查询超时，未确认新鲜度')
                self.cache[args] = failure
                raise failure from error
            except OSError as error:
                failure = ValueError('本机无法执行来源 Git 查询')
                self.cache[args] = failure
                raise failure from error
            if result.returncode:
                failure = ValueError('来源 Git 提交或仓库不可读，未确认新鲜度')
                self.cache[args] = failure
                raise failure
            self.cache[args] = result.stdout
        if isinstance(self.cache[args], ValueError):
            raise ValueError(str(self.cache[args]))
        return self.cache[args]

    def check(self, source):
        covers, commit = source['covers'], source['commit']
        result = source | {'state': 'unknown', 'current_commit': self.head,
                           'changed_paths': [], 'diagnostics': []}
        try:
            for pattern in covers:
                if not (self.path / _prefix(pattern)).resolve().is_relative_to(self.path):
                    raise ValueError('覆盖范围经符号链接越出来源仓库')
            baseline = self.run('rev-parse', '--verify', commit + '^{commit}').strip()
            result['commit'] = baseline
            changed = set(self.run('diff', '--no-ext-diff', '--no-textconv', '--no-renames',
                                   '--name-only', '-z', baseline, self.head, '--', *self.prefixes).split('\x00'))
            files = set(self.run('ls-tree', '-r', '--name-only', '-z', baseline, '--', *self.prefixes).split('\x00'))
            affected = sorted(path for path in changed | self.dirty if path and _matches(path, covers))
            known = {path for path in files | changed | self.dirty if path and _matches(path, covers)}
            if not known:
                raise ValueError('覆盖范围未匹配到可核验的仓库文件')
            if any(not (self.path / path).resolve().is_relative_to(self.path) for path in known):
                raise ValueError('覆盖文件经符号链接越出来源仓库')
            result.update(state='stale' if affected else 'current', changed_paths=affected)
        except (OSError, RuntimeError, ValueError) as error:
            result['diagnostics'].append(str(error))
        return result


def _metadata(root, path):
    # Reuse the document reader's allowlist, including symlink confinement.
    if not isinstance(path, str) or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
        raise ValueError('知识文档路径必须在工作区文档目录内')
    document = retrieval.document_path(root, path)
    sidecar = document.with_suffix('.json').resolve()
    if not sidecar.is_relative_to(root):
        raise ValueError('知识来源文件经符号链接越出工作区')
    if not sidecar.is_file():
        return None
    value = json.loads(sidecar.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('知识来源文件必须是 JSON object')
    return value


def _sources(metadata):
    knowledge = metadata.get('knowledge')
    if knowledge is None:
        return None, None
    if not isinstance(knowledge, dict):
        raise ValueError('knowledge 必须是 object')
    verified = knowledge.get('verified_at')
    if verified is not None and _time(verified) is None:
        raise ValueError('knowledge.verified_at 必须是带时区的 ISO 时间')
    sources = knowledge.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('knowledge.sources 必须是非空来源数组')
    normalized = []
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get('repository'), str) or not source['repository']:
            raise ValueError('来源 repository 必须是登记的仓库名称')
        commit, covers = source.get('commit'), source.get('covers')
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-fA-F]{4,64}', commit):
            raise ValueError('来源 commit 必须是固定的十六进制 Git 提交 ID')
        if not isinstance(covers, list) or not covers:
            raise ValueError('每个来源必须声明非空 covers 范围')
        normalized.append({'repository': source['repository'], 'commit': commit,
                           'covers': list(dict.fromkeys(_relative(pattern) for pattern in covers))})
    return normalized, verified


def freshness_index(root: Path, documents, events: list, config: dict) -> dict:
    """Check selected documents, sharing Git queries per repository and commit.

    New event candidates share a task with source events or explicitly supersede
    a source, including through a correction chain. For source records without
    task identity, repository membership is the available, broader hint. These
    hints can request review but never declare the document wrong.
    """
    root = root.resolve()
    checked_at = datetime.now(timezone.utc).isoformat()
    event_index = {event['event_id']: event for event in events if isinstance(event, dict) and event.get('event_id')}
    successors = {}
    for event in event_index.values():
        for parent in event.get('supersedes', []):
            successors.setdefault(parent, set()).add(event['event_id'])
    reports, pending, scopes = {}, {}, {}
    for document in documents:
        path = document['path'] if isinstance(document, dict) else document
        report = {'path': path, 'state': 'unknown', 'checked_at': checked_at, 'verified_at': None,
                  'source_event_ids': [], 'newer_event_ids': [], 'sources': [], 'diagnostics': []}
        reports[path] = report
        try:
            metadata = _metadata(root, path)
            if metadata is None:
                report['diagnostics'].append('没有知识来源元数据，不能根据文件修改时间判断新鲜度')
                continue
            raw = metadata.get('events', [])
            if not isinstance(raw, list):
                raise ValueError('来源 events 必须是数组')
            references = [item.get('event', item) for item in raw if isinstance(item, dict)]
            references = [item for item in references if isinstance(item, dict) and isinstance(item.get('event_id'), str)]
            report['source_event_ids'] = list(dict.fromkeys(item['event_id'] for item in references))
            originals = [event_index.get(item['event_id'], item) for item in references]
            sources, report['verified_at'] = _sources(metadata)
            if sources is None:
                report['diagnostics'].append('已有事件来源，但未声明知识覆盖的 Git 提交与文件范围')
                continue
            pending[path] = (sources, originals)
            for source in sources:
                scopes.setdefault(source['repository'], set()).update(source['covers'])
        except (OSError, RuntimeError, ValueError, TypeError) as error:
            report['diagnostics'].append(str(error))
    repositories = {}
    for name, covers in scopes.items():
        try:
            repositories[name] = _Git(_repository(root, config, name), covers)
        except (OSError, RuntimeError, ValueError) as error:
            repositories[name] = str(error)
    for path, (sources, originals) in pending.items():
        report = reports[path]
        for source in sources:
            repository = repositories[source['repository']]
            report['sources'].append(repository.check(source) if isinstance(repository, _Git) else source | {
                'state': 'unknown', 'current_commit': None, 'changed_paths': [], 'diagnostics': [repository],
            })
        states = {source['state'] for source in report['sources']}
        report['state'] = 'stale' if 'stale' in states else 'unknown' if 'unknown' in states else 'current'
        times = [time for item in originals if (time := _time(item.get('created_at'))) is not None]
        baseline = _time(report['verified_at']) or max(times, default=None)
        tasks = {item['task_id'] for item in originals if isinstance(item.get('task_id'), str)
                 and item['task_id'] not in ('', '-')}
        names = {source['repository'] for source in sources}
        if baseline is not None:
            related_ids = set(report['source_event_ids'])
            to_visit = list(related_ids)
            while to_visit:
                for child in successors.get(to_visit.pop(), ()):
                    if child not in related_ids:
                        related_ids.add(child)
                        to_visit.append(child)
            report['newer_event_ids'] = [event['event_id'] for event in events if (
                isinstance(event, dict) and event.get('event_id') not in report['source_event_ids']
                and (when := _time(event.get('created_at'))) is not None and when > baseline
                and (event.get('event_id') in related_ids or (event.get('task_id') in tasks if tasks else any(
                    isinstance(project, dict) and project.get('name') in names for project in event.get('projects', [])
                )))
            )]
            if report['newer_event_ids']:
                report['diagnostics'].append('发现晚于知识核验依据的关联事件，建议复核；这不证明现有结论错误')
                if report['state'] == 'current':
                    report['state'] = 'needs_review'
    return reports


def document_freshness(root: Path, path: str, events: list, config: dict) -> dict:
    return freshness_index(root, [path], events, config)[path]
