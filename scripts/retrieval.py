"""Shared lexical search and confined document reads; no index or dependencies."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unicodedata


DOCUMENT_DIRS = ('.share/knowledge', '.share/docs', 'docs', 'tools', 'examples')
ASSET_SUFFIXES = {'.md', '.svg', '.png', '.jpg', '.jpeg', '.webp'}


def folded(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


def query_groups(query: str, aliases: dict | None = None) -> list[tuple[str, ...]]:
    groups = []
    vocabulary = {}
    if aliases is not None and not isinstance(aliases, dict):
        raise ValueError("search_aliases 必须是 object")
    for canonical, variants in (aliases or {}).items():
        if not isinstance(canonical, str) or not canonical.strip() or not isinstance(variants, list) or any(
            not isinstance(v, str) or not v.strip() for v in variants
        ):
            raise ValueError('search_aliases 必须是非空名称到字符串数组的映射')
        names = tuple(dict.fromkeys(folded(v) for v in [canonical, *variants]))
        for name in names:
            vocabulary.setdefault(name, set()).update(names)
    for term in folded(query).split():
        groups.append(tuple(sorted(vocabulary.get(term, {term}))))
    return groups


def matches(text: str, groups: list[tuple[str, ...]]) -> bool:
    value = folded(text)
    return all(any(term in value for term in alternatives) for alternatives in groups)


def score(fields, groups: list[tuple[str, ...]]) -> int:
    return sum(weight for text, weight in fields for alternatives in groups
               if any(term in folded(text) for term in alternatives))


def excerpt(text: str, groups, size: int = 180) -> str:
    # Keep the matching line readable; matching itself does not require Chinese tokenization.
    lines = text.splitlines()
    line = max(lines, key=lambda s: sum(any(t in folded(s) for t in g) for g in groups), default='')
    # Display NFKC text; map case-fold expansions back to these display positions.
    # E.g. ß consumes two searchable characters but one displayed character.
    value = ' '.join(unicodedata.normalize('NFKC', line).split())
    searchable, positions = [], []
    for index, char in enumerate(value):
        part = char.casefold()
        searchable.append(part)
        positions.extend([index] * len(part))
    search_text = ''.join(searchable)
    hits = [search_text.find(t) for g in groups for t in g if t in search_text]
    match = positions[min(hits)] if hits else 0
    start = max(0, match - size // 3)
    return ('…' if start else '') + value[start:start + size] + ('…' if len(value) > start + size else '')


def document_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    root = root.resolve()
    path = (root / value).resolve()
    # A symlinked document root cannot grant access outside the workspace.
    allowed = path.is_relative_to(root) and (
        any(path.is_relative_to(root / name) for name in DOCUMENT_DIRS) or path == root / 'README.md'
    )
    if not allowed or path.suffix.lower() not in ASSET_SUFFIXES:
        raise PermissionError('只能读取文档目录中的 Markdown 和图片')
    if must_exist and not path.is_file():
        raise FileNotFoundError('文档不存在')
    return path


def scan_documents(root: Path) -> list[dict]:
    root = root.resolve()
    paths = [root / 'README.md']
    for name in DOCUMENT_DIRS:
        base = root / name
        if base.is_dir() and base.resolve().is_relative_to(root):
            paths.extend(base.rglob('*.md'))
            paths.extend(base.rglob('*.svg'))
    result = []
    for candidate in sorted(set(paths)):
        relative = candidate.relative_to(root).as_posix()
        try:
            path = document_path(root, relative)
        except (PermissionError, FileNotFoundError):
            continue
        text = path.read_text(encoding='utf-8', errors='replace') if path.suffix == '.md' else ''
        title = next((s[2:].strip() for s in text.splitlines()[:20] if s.startswith('# ')), path.stem)
        result.append({'path': relative, 'title': title, 'kind': path.suffix[1:], 'text': text,
                       'updated_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()})
    return result


def search_documents(documents: list[dict], query: str = '', *, aliases=None, sort='relevance') -> list[dict]:
    if sort not in {'relevance', 'recent'}:
        raise ValueError('未知排序')
    groups = query_groups(query, aliases)
    selected = [d for d in documents if matches(d['title'] + '\n' + d['path'] + '\n' + d['text'], groups)]
    if groups or sort == 'recent':
        selected.sort(key=lambda d: (score([(d['title'], 8), (d['path'], 4), (d['text'], 1)], groups)
                                    if sort == 'relevance' else 0, d['updated_at'], d['path']), reverse=True)
    result = []
    for document in selected:
        item = {k: v for k, v in document.items() if k != 'text'}
        if groups:
            for field, label in (('text', ''), ('title', '标题：'), ('path', '路径：')):
                if any(term in folded(document[field]) for group in groups for term in group):
                    item['excerpt'] = label + excerpt(document[field], groups)
                    break
        result.append(item)
    return result
