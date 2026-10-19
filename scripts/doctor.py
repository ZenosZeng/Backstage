"""Read-only local diagnostics using the same validators and link plan as sync."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import shutil

try:
    from scripts import memory, retrieval, sync
except ModuleNotFoundError:
    import memory
    import retrieval
    import sync


def inspect_workspace(root: Path, *, home: Path | None = None, local: Path | None = None) -> dict:
    root = root.resolve()
    local = (local or root / ".local").resolve()
    checks = []
    def add(name, state, detail, hint=''):
        checks.append({'name': name, 'state': state, 'detail': detail, 'hint': hint})
    try:
        config = memory.load_config(root)
        retrieval.query_groups('', config.get('search_aliases', {}))
        if not memory.workspace_root(config).is_dir():
            raise ValueError('Invalid workspace configuration')
        add('配置', 'ok', '工作区身份、项目路径与搜索别名格式有效')
    except (OSError, ValueError, TypeError, AttributeError):
        config = None
        add('配置', 'error', '配置缺失、不可读或字段无效', '检查 config.json；自检不输出配置值或凭据。')
    sources = {}
    try:
        events, errors = memory.load_events(root, sources=sources)
        lifecycle = memory.lifecycle_reference_errors(events)
        cycles = memory.supersedes_cycles(events)
        add('记忆校验', 'error' if errors or lifecycle or cycles else 'ok',
            f'{len(events)} 条事件；{len(errors)} 个读取/校验错误；{len(lifecycle)} 个生命周期错误；{len(cycles)} 个循环节点',
            '运行 memory validate / audit 核对来源。' if errors or lifecycle or cycles else '')
        args = argparse.Namespace(limit=None, stale_days=60)
        report = memory.audit_events(events, errors, sources, args)
        labels = {'missing_source':'缺少来源', 'stale_candidate':'长期未核对',
                  'dangling_supersedes':'修正来源缺失', 'parallel_supersedes_candidate':'并行修正待核对',
                  'supersedes_cycle':'修正关系成环', 'legacy_date_mismatch':'历史日期错位',
                  'legacy_non_utc':'历史非 UTC 时间', 'task_state_conflict':'任务状态冲突',
                  'invalid_lifecycle_reference':'生命周期引用无效', 'lifecycle_cycle':'生命周期成环',
                  'invalid':'读取或格式错误'}
        counts_text = '；'.join(f"{labels.get(k, '其他待核对项')} {n} 项" for k, n in report['counts'].items())
        add('记忆审计', 'warning' if report['issues'] else 'ok', counts_text or '未发现审计候选问题',
            '审计只列候选问题；不会删除历史或自动裁决冲突。' if report['issues'] else '')
    except (OSError, ValueError, KeyError, TypeError):
        events = []
        add('记忆校验', 'error', '无法完整读取记忆', '检查 memory validate 的结果。')
    if config:
        try:
            links = sync.agent_links(root, config, home=home)
            missing, wrong = [], []
            for path, target in links:
                try:
                    if not path.exists() and not path.is_symlink():
                        missing.append(path)
                    elif not target.exists() or path.resolve() != target.resolve():
                        wrong.append(path)
                except (OSError, RuntimeError):
                    wrong.append(path)
            add('Skill 与入口链接', 'warning' if missing or wrong else 'ok',
                f'{len(links)} 个预期链接；缺失 {len(missing)}；来源不符或断链 {len(wrong)}',
                '按初始化说明核对链接；保留现有独立配置，不自动覆盖。' if missing or wrong else '')
        except (OSError, sync.SyncError):
            add('Skill 与入口链接', 'warning', '共享 Skill 缺失、重复或不可读', '检查 .share/agent/skills 与入口 Prompt。')
        prompts = [root / '.share/agent/prompts' / name for name in ('AGENTS.md', 'CLAUDE.md')]
        try:
            equal = prompts[0].read_bytes() == prompts[1].read_bytes()
            add('入口一致性', 'ok' if equal else 'warning', '两份入口全文一致' if equal else '两份入口内容不同')
        except OSError:
            add('入口一致性', 'warning', '入口 Prompt 尚未完整配置')
        sync_config = config.get('sync', {})
        if not isinstance(sync_config, dict):
            add('同步', 'error', 'sync 字段必须是 object')
        elif not sync_config.get('enabled'):
            add('同步', 'unconfigured', '本地模式；没有启用 S3 同步')
        else:
            try:
                sync.load_config(root, require_initialized=False)
                conflict = (local / '.sync/conflict').is_dir() or (local / '.sync/raw-conflict.json').exists()
                base = local / '.sync/base'
                problems = []
                if not sync_config.get('initialized'):
                    problems.append('尚未初始化')
                if conflict:
                    problems.append('存在本机冲突证据')
                if not base.is_dir():
                    problems.append('缺少 shared 基线')
                if not shutil.which('mc'):
                    problems.append('当前 PATH 未发现 mc')
                add('同步', 'warning' if problems else 'ok', '；'.join(problems) or '本机初始化、基线和客户端检查通过',
                    '只核对本机文件；未检查远端连通性或推断同步成功。')
            except (OSError, sync.SyncError):
                add('同步', 'error', '同步配置无效', '检查 sync 字段；自检不连接存储。')
    latest = {}
    for e in events:
        key = (e['machine_id'], e['agent'])
        latest[key] = e['created_at']
    counts = Counter(c['state'] for c in checks)
    return {'state': 'error' if counts['error'] else 'warning' if counts['warning'] else 'ok',
            'sampled_at': datetime.now(timezone.utc).isoformat(), 'scope': 'local', 'local_root': str(local), 'checks': checks,
            'last_events': [{'machine': m, 'agent': a, 'created_at': at} for (m, a), at in sorted(latest.items())],
            'notice': '只读本机自检；最后记忆时间不代表机器在线或远端同步成功。'}
