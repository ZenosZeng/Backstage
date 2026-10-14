#!/usr/bin/env python3
"""Feishu transport, authorization and command routing for Claude / DSH.

双向遥控：在飞书里私聊机器人（或群聊 @它）发指令 → lark-oapi 长连接
（WS，无需公网回调）接收 → 白名单校验 → 本机 Agent CLI 执行 →
结果回复到原消息线程。

安全设计（codex 审计 2026-08-07 后修复）：
- 凭证只从环境变量读（start 从 0600 的 ~/.config/claude-feishu/env 加载）
- 白名单为空时拒绝启动（除非显式 FEISHU_OPEN_MODE=1）
- claude 子进程使用最小 env 白名单 + 独立 settings（--settings）
- 超时按进程组 TERM/KILL（start_new_session）
- 日志不记录 prompt 原文（只记长度+哈希）；SDK 日志降级 WARN

运行：在 .agents 下使用 pixi run claude-feishu；配置见 tools/README.md。
"""

from __future__ import annotations

import os
import sys
import json
import time
import hashlib
import logging
import pathlib
import shutil
import subprocess
import threading
import urllib.request
from typing import Callable

from tools.claude_feishu import harness, presentation, runner
from tools.claude_feishu.sessions import SessionStore
from tools.claude_feishu.presentation import CARD_BODY_MAX

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
HOME_DIR = pathlib.Path.home()
from collections import deque

from lark_oapi import EventDispatcherHandler
from lark_oapi.core.enum import LogLevel
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
import lark_oapi.ws as ws

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BOT_OPEN_ID = os.environ.get("FEISHU_BOT_OPEN_ID", "")  # 群聊提及校验
CLAUDE = os.environ.get(
    "CLAUDE_CLI",
    shutil.which("claude") or str(HOME_DIR / ".pixi/envs/nodejs/bin/claude"),
)
SETTINGS = os.environ.get(
    "CLAUDE_FEISHU_SETTINGS", str(SCRIPT_DIR / "claude-settings.json")
)
WORKDIR = os.environ.get("CLAUDE_FEISHU_WORKDIR", str(HOME_DIR / "code"))
ALLOW_OPEN_IDS = {
    s for s in os.environ.get("FEISHU_ALLOW_OPEN_IDS", "").split(",") if s
}
# 审计 C3：默认 fail-closed；显式 FEISHU_OPEN_MODE=1 才允许空白名单（不推荐）
OPEN_MODE = os.environ.get("FEISHU_OPEN_MODE", "") == "1"
MAX_REPLY = 3800  # 单条消息字符上限（长回复分块发送）
DSH_TIMEOUT = int(os.environ.get("CLAUDE_FEISHU_DSH_TIMEOUT", "600"))
EXEC_TIMEOUT = int(os.environ.get("CLAUDE_FEISHU_TIMEOUT", "600"))  # 单条指令上限
# 流式进度：解析 claude 的 stream-json 事件，按此间隔刷新同一张飞书卡片。
# 设 0 关闭中间刷新，仍发送开始/结束卡片；兼容旧 STREAM 环境变量。
STREAM_INTERVAL = int(os.environ.get(
    "CLAUDE_FEISHU_PROGRESS_INTERVAL", os.environ.get("CLAUDE_FEISHU_STREAM_INTERVAL", "5")
))
QUEUE_MAX = 5  # 每个 chat 的排队指令上限
def _read_settings_env() -> dict:
    """~/.claude/settings.json 的 env 块——CLI 实际生效的配置来源。

    注意不是 --settings 指向的那个文件（claude-settings.json 只有 129B、无 env
    块）；模型与 effort 都配在 ~/.claude/settings.json 里。
    """
    try:
        data = json.loads((HOME_DIR / ".claude/settings.json").read_text())
    except Exception:  # noqa: BLE001 - 读不到就当没配，不影响执行
        return {}
    env = data.get("env")
    return env if isinstance(env, dict) else {}


_SETTINGS_ENV = _read_settings_env()


def _setting(name: str, default: str = "") -> str:
    """取生效配置：进程环境优先，其次 ~/.claude/settings.json 的 env 块。

    effort 等变量只配在 settings.json 里、不在 bridge 进程环境里，只读
    os.environ 会取空。
    """
    return os.environ.get(name) or str(_SETTINGS_ENV.get(name) or default)


# 上下文窗口大小：优先取 CLI 实际生效的 CLAUDE_CODE_MAX_CONTEXT_TOKENS，
# 没有就只报已用量、不报百分比（避免拿错窗口算出误导性的占比）。
CONTEXT_WINDOW = int(_setting("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "0") or 0)
# DSH 的事件流不报窗口（见 harness.py），只能由配置声明；未配置时同样只报绝对量。
# 取值应与该 profile 实际解析到的模型一致，配错会算出误导性的百分比。
DSH_CONTEXT_WINDOW = int(_setting("CLAUDE_FEISHU_DSH_CONTEXT_WINDOW", "0") or 0)
EFFORT = _setting("CLAUDE_CODE_EFFORT_LEVEL")


# 打断词：执行中再发这些内容即终止当前进程组（其余消息仍回"忙"提示）
INTERRUPT_WORDS = {
    w.strip().lower()
    for w in os.environ.get("CLAUDE_FEISHU_INTERRUPT_WORDS", "停,停止,打断,中断,取消,stop,cancel").split(",")
    if w.strip()
}
# 去重持久化（审计 H7：重启后不丢）
SEEN_FILE = os.environ.get(
    "CLAUDE_FEISHU_STATE", str(HOME_DIR / ".local/state/claude-feishu-seen.json")
)
# 会话上下文：按 chat 持久化 claude 原生会话（-r/--session-id），跨消息有上下文。
# 仅保存目录、原生会话 ID 与累计用量；对话本体由各 Agent CLI 自己管理；
# 设 FEISHU_SESSION_CONTEXT=0 可关闭，退回每次全新会话的旧行为。
SESSION_FILE = os.environ.get(
    "CLAUDE_FEISHU_SESSIONS", str(HOME_DIR / ".local/state/claude-feishu-sessions.json")
)
SESSION_CONTEXT_ENABLED = os.environ.get("FEISHU_SESSION_CONTEXT", "1") != "0"


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0, got {value}")
    return value


SEEN_LIMIT = _positive_int_env("FEISHU_SEEN_LIMIT", 2000)
MAX_EVENT_AGE_SECONDS = _positive_int_env("FEISHU_MAX_EVENT_AGE_SECONDS", 30)
_STARTED_AT_MS = int(time.time() * 1000)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
_lock = threading.Lock()  # 同时只执行一条遥控指令
_queue_lock = threading.Lock()
_chat_queues: dict[str, deque] = {}  # chat_id -> 排队的 (event, 指令)
_drain_running = False  # 单消费者：同一时刻最多一个 drain 线程（_queue_lock 保护）
_token_lock = threading.Lock()
_restart_pending = threading.Event()
_service_ready: Callable[[], None] | None = None
_service_config_path: str = ""
_token: dict[str, object] = {"token": "", "expires": 0.0}


# ---------------------------------------------------------------------------
# 防重放：持久化 message_id、event_id 和稳定事件指纹
# ---------------------------------------------------------------------------


def _load_seen() -> tuple[list[str], list[str], list[str]]:
    state_path = pathlib.Path(SEEN_FILE)
    if not state_path.exists():
        return [], [], []
    try:
        data = json.loads(state_path.read_text())
        if isinstance(data, list):  # v1: message_id list
            if all(isinstance(item, str) for item in data):
                return list(data), [], []
            raise ValueError("legacy state contains non-string message IDs")
        if not isinstance(data, dict) or data.get("version") != 2:
            raise ValueError("unsupported state schema")
        identity_lists = tuple(
            data.get(key, []) for key in ("message_ids", "event_ids", "fingerprints")
        )
        if not all(
            isinstance(items, list) and all(isinstance(item, str) for item in items)
            for items in identity_lists
        ):
            raise ValueError("state identity lists must contain only strings")
        return identity_lists
    except Exception as exc:  # noqa: BLE001
        # An unreadable dedup ledger must never silently become an empty ledger.
        raise RuntimeError(f"invalid seen state {state_path}: {exc}") from exc


_loaded_message_ids, _loaded_event_ids, _loaded_fingerprints = _load_seen()
_seen_msg_ids: deque[str] = deque(_loaded_message_ids, maxlen=SEEN_LIMIT)
_seen_event_ids: deque[str] = deque(_loaded_event_ids, maxlen=SEEN_LIMIT)
_seen_fingerprints: deque[str] = deque(_loaded_fingerprints, maxlen=SEEN_LIMIT)
_seen_lock = threading.Lock()


def _event_fingerprint(data: P2ImMessageReceiveV1) -> str:
    msg = data.event.message
    sender_id = (
        getattr(getattr(data.event.sender, "sender_id", None), "open_id", "") or ""
    )
    payload = json.dumps(
        {
            "sender": sender_id,
            "chat": msg.chat_id or "",
            "created": str(
                msg.create_time or getattr(data.header, "create_time", "") or ""
            ),
            "type": msg.message_type or "",
            "content": msg.content or "",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _event_created_at_ms(data: P2ImMessageReceiveV1) -> int | None:
    values = (
        getattr(data.event.message, "create_time", None),
        getattr(getattr(data, "header", None), "create_time", None),
    )
    for value in values:
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            continue
        if timestamp < 10_000_000_000:
            timestamp *= 1000
        return timestamp
    return None


def _stale_event_reason(
    data: P2ImMessageReceiveV1, *, now_ms: int | None = None
) -> str | None:
    created_at_ms = _event_created_at_ms(data)
    if created_at_ms is None:
        return "missing create_time"
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if created_at_ms < _STARTED_AT_MS - 5000:
        return "predates bridge startup"
    age_ms = current_ms - created_at_ms
    if age_ms > MAX_EVENT_AGE_SECONDS * 1000:
        return f"age={age_ms / 1000:.1f}s"
    if age_ms < -30_000:
        return f"future timestamp={-age_ms / 1000:.1f}s"
    return None


def _persist_seen() -> None:
    state_path = pathlib.Path(SEEN_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    payload = {
        "version": 2,
        "message_ids": list(_seen_msg_ids),
        "event_ids": list(_seen_event_ids),
        "fingerprints": list(_seen_fingerprints),
    }
    temp_path.write_text(json.dumps(payload, separators=(",", ":")))
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, state_path)


def _mark_seen(msg_id: str, event_id: str, fingerprint: str) -> bool:
    """Atomically persist all stable event identities before execution."""
    with _seen_lock:
        if (
            (msg_id and msg_id in _seen_msg_ids)
            or (event_id and event_id in _seen_event_ids)
            or (fingerprint and fingerprint in _seen_fingerprints)
        ):
            return False
        if msg_id:
            _seen_msg_ids.append(msg_id)
        if event_id:
            _seen_event_ids.append(event_id)
        if fingerprint:
            _seen_fingerprints.append(fingerprint)
        _persist_seen()
        return True


# ---------------------------------------------------------------------------
# token（审计 M9：加锁 + 异常不外逃）
# ---------------------------------------------------------------------------


def _get_token() -> str:
    with _token_lock:
        if _token["expires"] > time.time() + 60:
            return str(_token["token"])
        payload = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        if body.get("code") != 0:
            raise RuntimeError(
                f"token error: code={body.get('code')} msg={body.get('msg')}"
            )
        _token["token"] = body["tenant_access_token"]
        _token["expires"] = time.time() + body["expire"] - 60
        return str(_token["token"])


# ---------------------------------------------------------------------------
# 回复（审计 M8：用 reply API 真正挂线程；L13：长回复分块）
# ---------------------------------------------------------------------------


def _post_reply(data: P2ImMessageReceiveV1, text: str) -> bool:
    msg = data.event.message
    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.message_id}/reply"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_get_token()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        if body.get("code") != 0:
            logging.error("reply failed: %s", body)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logging.error("reply exception: %s", exc)
        return False


def _extract_message_id(body: dict) -> str | None:
    data = body.get("data") if isinstance(body, dict) else None
    return (data or {}).get("message_id") or None


# header.subtitle 是卡片 1.0 的可选字段；万一服务端不认，就降级成不带副标题的
# 卡片，而不是整张卡片发不出去、直接退化成纯文本。
_subtitle_supported = True


def _without_subtitle(card: dict) -> dict:
    header = {k: v for k, v in (card.get("header") or {}).items() if k != "subtitle"}
    subtitle = (card.get("header") or {}).get("subtitle")
    elements = list(card.get("elements") or [])
    if subtitle:
        elements.insert(0, {"tag": "div", "text": {"tag": "plain_text", "content": subtitle["content"]}})
    return {**card, "header": header, "elements": elements}


def _card_api(url: str, card: dict, method: str) -> tuple[bool, dict]:
    """调用飞书卡片接口（POST reply / PATCH 更新），返回 (是否成功, 响应体)。"""
    data = {"content": json.dumps(card, ensure_ascii=False)}
    if method == "POST":
        data = {"msg_type": "interactive", **data}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_get_token()}",
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        logging.error("card %s exception: %s", method, exc)
        return False, {}
    if body.get("code") != 0:
        logging.error("card %s failed: %s", method, body)
        return False, body
    return True, body


def _call_card_api(url: str, card: dict, method: str) -> tuple[bool, dict]:
    """带副标题降级：被拒时去掉 subtitle 重试一次，并记住以后不再带。"""
    global _subtitle_supported
    ok, body = _card_api(url, card, method)
    if ok or not _subtitle_supported or "subtitle" not in (card.get("header") or {}):
        return ok, body
    logging.warning("card %s rejected with header.subtitle; retrying without", method)
    ok, body = _card_api(url, _without_subtitle(card), method)
    if ok:
        _subtitle_supported = False
        logging.warning("header.subtitle unsupported; statusline dropped from cards")
    return ok, body


def _post_card_reply(data: P2ImMessageReceiveV1, card: dict) -> str | None:
    """以交互卡片形式回复，返回新消息 id（供后续原地更新）；失败返回 None。"""
    msg = data.event.message
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.message_id}/reply"
    ok, body = _call_card_api(url, card, "POST")
    return _extract_message_id(body) if ok else None


def _patch_card(message_id: str, card: dict) -> bool:
    """原地更新一张已发出的卡片（PATCH /im/v1/messages/{id}）。"""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
    ok, _ = _call_card_api(url, card, "PATCH")
    return ok


class RunState(presentation.RunState):
    """Bind bridge display settings to one observed Agent run."""

    def __init__(self, harness_name: str = "claude") -> None:
        super().__init__(
            harness_name,
            context_window=CONTEXT_WINDOW if harness_name == "claude" else DSH_CONTEXT_WINDOW,
            default_workdir=WORKDIR, home_dir=HOME_DIR, effort=EFFORT,
            claude_model=_setting("ANTHROPIC_MODEL"),
        )


def _short_path(path: str) -> str:
    return presentation.short_path(path, HOME_DIR)


def _card(state: RunState, elapsed: float, *, done=None, starting=False) -> dict:
    return presentation.card(
        state, elapsed, done=done, starting=starting,
        subtitle_supported=_subtitle_supported,
    )


def _send_reply(data: P2ImMessageReceiveV1, text: str) -> bool:
    """Reply into the same chat thread; long text split into chunks."""
    if not text:
        return True
    chunks = [text[i : i + MAX_REPLY] for i in range(0, len(text), MAX_REPLY)]
    ok = True
    for index, chunk in enumerate(chunks, 1):
        body = chunk if len(chunks) == 1 else f"({index}/{len(chunks)})\n{chunk}"
        ok = _post_reply(data, body) and ok
    return ok


# ---------------------------------------------------------------------------
# 会话上下文（2026-08-07）：chat_id -> claude session_id
# 与 seen 账本不同，本文件损坏只丢上下文、不丢安全属性，故 fail-open 仅告警。
# 命令与执行线程共享状态，读改写和原子落盘由 sessions.lock 保护。
# ---------------------------------------------------------------------------


sessions = SessionStore(SESSION_FILE)
agent_runner = runner.AgentRunner(sessions)





def _chat_workdir(chat_id: str) -> str:
    return sessions.workdir(chat_id, WORKDIR)


# ---------------------------------------------------------------------------
# claude 子进程（审计 C2：最小 env 白名单；H6：进程组终止；H5：独立 settings）
# ---------------------------------------------------------------------------

# 只保留运行 claude 必需的变量；FEISHU_*/AWS_*/云存储/Coder 凭证一律不带


def _run_agent(prompt: str, chat_id: str = "", on_progress=None, *, harness_name="claude"):
    return agent_runner.run(
        prompt, chat_id, on_progress, harness_name=harness_name,
        claude_cli=CLAUDE, settings=SETTINGS, default_workdir=WORKDIR,
        session_context_enabled=SESSION_CONTEXT_ENABLED,
        timeout=DSH_TIMEOUT if harness_name == "dsh" else EXEC_TIMEOUT,
        progress_interval=STREAM_INTERVAL, state_factory=RunState,
    )


def _request_interrupt() -> bool:
    return agent_runner.interrupt()


def shutdown() -> None:
    agent_runner.shutdown()


def _run_claude(prompt, chat_id="", on_progress=None):
    return _run_agent(prompt, chat_id, on_progress, harness_name="claude")


def _run_dsh(prompt, chat_id="", on_progress=None):
    return _run_agent(prompt, chat_id, on_progress, harness_name="dsh")


def _is_interrupt(text: str) -> bool:
    return text.strip().lower() in INTERRUPT_WORDS


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------


def _strip_mention(data: P2ImMessageReceiveV1, text: str) -> str:
    """Remove mention placeholders using the exact mention keys (审计 M11)."""
    for mention in data.event.message.mentions or []:
        key = getattr(mention, "key", "") or ""
        if key:
            text = text.replace(key, "")
    # 兜底：@机器人显示名
    for marker in ("@Coder Claude Remote", "@Claude Remote"):
        text = text.replace(marker, "")
    return text.strip()


def _drain_queues() -> None:
    """唯一消费者线程：所有 chat 的排队指令都由它顺序执行。

    审计 P2：此前 _kick_queue 与收尾都能起消费者，而出队与获取执行锁不是原子
    的——两个消费者各弹一条后抢锁，先排的 A 可能后执行。现在全进程只允许一个
    消费者（_drain_running 在 _queue_lock 内判定与释放），出队即执行顺序。
    """
    global _drain_running
    while True:
        with _queue_lock:
            item = None
            for chat_id in list(_chat_queues):
                pending = _chat_queues.get(chat_id)
                if not pending:
                    continue
                item = (chat_id, pending.popleft())
                if pending:
                    # 还有剩余就轮转到队尾（dict 没有 move_to_end，重插即轮转）
                    _chat_queues.pop(chat_id)
                    _chat_queues[chat_id] = pending
                else:
                    _chat_queues.pop(chat_id, None)
                break
            if item is None:
                _drain_running = False  # 与"确认空队列"同锁，避免漏唤醒
                return
        chat_id, (item_data, item_text) = item
        _execute_and_reply(item_data, item_text, chat_id, queued=True)


def _kick_queue() -> None:
    """确保有消费者在跑；已有则什么都不做（空闲/忙时都成立）。"""
    global _drain_running
    with _queue_lock:
        if _drain_running:
            return
        _drain_running = True
    threading.Thread(target=_drain_queues, daemon=True).start()


def _queue_position(chat_id: str) -> int:
    with _queue_lock:
        return len(_chat_queues.get(chat_id) or ())


HELP_TEXT = (
    "🤖 **Claude / DSH 遥控**\n"
    "直接发指令即可执行（群聊需 @机器人，私聊直接发）。\n\n"
    "**命令**\n"
    "- `/claude` / `/dsh` 粘性切换，两套会话独立保存\n"
    "- `/new [claude|dsh|all]` 清空指定会话（默认当前 harness）\n"
    "- `/status` 会话状态（目录/上下文/队列/累计）\n"
    "- `/restart` 校验新代码后重启 bridge，保留会话并回复就绪结果\n"
    "- `/cd <目录>` 切换工作目录\n"
    "- `/cost` 本会话累计成本与轮次\n"
    "- `/queue <指令>` 排队执行（当前任务结束后自动开始）\n"
    "- `/help` 本帮助\n\n"
    "**执行中**\n"
    "- 发「停」「停止」「打断」「取消」「stop」「cancel」→ 打断当前任务\n"
    "- 其他消息会提示忙；想接着干活用 `/queue <指令>`\n"
    "- 排队指令按执行时的 harness 运行\n"
    "- DSH 按 commit 块刷新，无逐字流式，也不报告成本\n"
    f"- Claude 最长 {EXEC_TIMEOUT // 60} 分钟，DSH 最长 {DSH_TIMEOUT // 60} 分钟；进度卡片每 {STREAM_INTERVAL}s 原地刷新"
)


def _cmd_harness(data, chat_id, name, arg=""):
    if arg:
        _send_reply(data, f"用法：`/{name}` 切换后，再单独发送指令。")
        return
    if name == "dsh" and not harness.REGISTRY[name].available():
        _send_reply(
            data,
            "❌ 本机没有找到 dsh（未配置 DSH_CLI，PATH 里也没有）。装好后重启服务即可用；现在继续走 /claude。",
        )
        return
    with sessions.lock:
        sessions.entry(chat_id, create=True)["harness"] = name
        sid = sessions.bucket(chat_id, name).get("session_id")
        sessions.persist()
    _send_reply(data, f"已切换到 {name}；会话：{sid or '下条指令新建'}。")


def _cmd_status(data: P2ImMessageReceiveV1, chat_id: str) -> None:
    selected = sessions.harness_name(chat_id)
    lines = [
        "📊 **会话状态**",
        f"- 当前 harness：{selected}",
        f"- 目录：{_short_path(_chat_workdir(chat_id))}",
        f"- 队列：{_queue_position(chat_id)} 条待执行",
    ]
    for name in ("claude", "dsh"):
        entry = sessions.bucket(chat_id, name)
        used = int(entry.get("ctx_used") or 0)
        ctx = (
            f"{used / CONTEXT_WINDOW * 100:.0f}%（{used / 1000:.0f}k）"
            if name == "claude" and used and CONTEXT_WINDOW
            else f"{used / 1000:.0f}k"
            if used
            else "未知"
        )
        cost = (
            f"${float(entry.get('cost_usd') or 0):.4f}"
            if name == "claude"
            else "成本未上报"
        )
        lines.extend(
            [
                f"- {name}：{entry.get('session_id') or '未建立'} · CLI {'可用' if harness.REGISTRY[name].available() else '未安装'}",
                f"  上下文：{ctx} · 最近执行：{entry.get('last_used') or '无'}",
                f"  累计：{int(entry.get('turns_total') or 0)} 轮 · {cost} · {float(entry.get('seconds_total') or 0) / 60:.1f}min",
            ]
        )
        if (
            name == "dsh"
            and entry.get("cwd")
            and entry["cwd"] != _chat_workdir(chat_id)
        ):
            lines.append("⚠️ DSH 会话目录与当前目录不同，续跑将重建会话。")
    _send_reply(data, "\n".join(lines))


def _cmd_cost(data: P2ImMessageReceiveV1, chat_id: str) -> None:
    name = sessions.harness_name(chat_id)
    entry = sessions.bucket(chat_id, name)
    other = "claude" if name == "dsh" else "dsh"
    cost = (
        f"${float(entry.get('cost_usd') or 0):.4f}"
        if name == "claude"
        else "dsh 不上报"
    )
    output = int(entry.get("total_usage", {}).get("output_tokens") or 0)
    _send_reply(
        data,
        f"💰 **{name} 本会话累计**\n"
        f"- 轮次：{int(entry.get('turns_total') or 0)}\n"
        f"- 输出：{output} tok\n- 成本：{cost}\n"
        f"- 执行时长：{float(entry.get('seconds_total') or 0) / 60:.1f}min\n"
        f"另一边 {other} 累计 {sessions.bucket(chat_id, other).get('turns_total', 0)} 轮；/{other} 后 /cost 查看。",
    )


def _cmd_cd(data: P2ImMessageReceiveV1, chat_id: str, arg: str) -> None:
    if not arg:
        _send_reply(data, "用法：`/cd <目录>`（例：/cd ~/code/BEHAVIOR-1K）")
        return
    target = pathlib.Path(os.path.expanduser(arg))
    if not target.is_dir():
        _send_reply(data, f"❌ 目录不存在：{arg}")
        return
    resolved = str(target.resolve())
    with sessions.lock:
        entry = sessions.entry(chat_id, create=True)
        entry["workdir"] = resolved
        sessions.persist()
    _send_reply(data, f"📁 工作目录已切到 {_short_path(resolved)}")


def _cmd_new(data: P2ImMessageReceiveV1, chat_id: str, arg: str = "") -> None:
    if arg and arg not in {"claude", "dsh", "all"}:
        _send_reply(data, "用法：`/new [claude|dsh|all]`")
        return
    names = ("claude", "dsh") if arg == "all" else (arg or sessions.harness_name(chat_id),)
    sessions.reset(chat_id, names)
    with _queue_lock:
        _chat_queues.pop(chat_id, None)
    _send_reply(data, f"🆕 已清空 {' / '.join(names)} 会话，下条消息从空上下文开始。")


def _cmd_queue(data: P2ImMessageReceiveV1, chat_id: str, arg: str) -> None:
    if not arg:
        _send_reply(data, "用法：`/queue <指令>`——当前任务结束后自动执行。")
        return
    with _queue_lock:
        pending = _chat_queues.setdefault(chat_id, deque())
        if len(pending) >= QUEUE_MAX:
            position = None
        else:
            pending.append((data, arg))
            position = len(pending)
    if position is None:
        _send_reply(data, f"❌ 队列已满（上限 {QUEUE_MAX} 条），等前面跑完再排。")
        return
    if _lock.locked():
        _send_reply(data, f"📋 已排队（第 {position} 位），当前任务结束后自动开始。")
    else:
        _send_reply(data, "▶️ 当前空闲，排队指令立即开始。")
    _kick_queue()


def _cmd_restart(data, arg=""):
    if arg:
        _send_reply(data, "用法：`/restart`，不带参数。")
        return
    if not _lock.acquire(blocking=False):
        _send_reply(data, "⏳ 当前任务仍在执行。请等完成或发「停」，然后再发 /restart。")
        return
    try:
        with _queue_lock:
            queued = any(_chat_queues.values())
        if _restart_pending.is_set() or queued:
            _send_reply(data, "⏳ 已在重启或仍有排队任务，请完成后再试。")
            return
        if _service_ready is None:
            _send_reply(data, "⚠️ 请使用 Pixi 管理的 bridge 服务启动后再重启。")
            return
        if not _send_reply(data, "🔄 正在校验新代码并重启 bridge，就绪后会回复本消息。"):
            return
        _restart_pending.set()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "tools.claude_feishu", "--restart", "--config", _service_config_path,
                 "--reply-message", data.event.message.message_id],
                cwd=str(SCRIPT_DIR.parents[1]), stdin=subprocess.DEVNULL,
                start_new_session=True, close_fds=True,
            )
        except OSError:
            _restart_pending.clear()
            _send_reply(data, "⚠️ 无法提交重启请求，旧 bridge 仍在运行。")
            return

        def finish():
            code = proc.wait()
            # If validation was rejected, the old bridge continues accepting work.
            _restart_pending.clear()
            if code != 0:
                _send_reply(data, "⚠️ 重启未完成。请检查新代码或配置；详情见 .agents/.local/tools/claude-feishu/service.log。")

        threading.Thread(target=finish, daemon=True).start()
    finally:
        _lock.release()


def _handle_command(data: P2ImMessageReceiveV1, text: str, chat_id: str) -> bool:
    """命令拦截；返回 True 表示已处理、不再进 claude。"""
    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if _restart_pending.is_set() and cmd not in ("/status", "/help", "/h", "/?"):
        _send_reply(data, "🔄 bridge 正在重启，就绪后再发送指令。")
        return True
    if cmd in ("/help", "/h", "/?"):
        _send_reply(data, f"当前 harness：{sessions.harness_name(chat_id)}\n\n" + HELP_TEXT)
    elif cmd in ("/new", "/clear"):
        _cmd_new(data, chat_id, arg)
    elif cmd in ("/claude", "/dsh"):
        _cmd_harness(data, chat_id, cmd[1:], arg)
    elif cmd == "/status":
        _cmd_status(data, chat_id)
    elif cmd == "/restart":
        _cmd_restart(data, arg)
    elif cmd == "/cost":
        _cmd_cost(data, chat_id)
    elif cmd == "/cd":
        _cmd_cd(data, chat_id, arg)
    elif cmd in ("/queue", "/q"):
        _cmd_queue(data, chat_id, arg)
    else:
        _send_reply(data, f"未知命令 {cmd}，发 /help 看可用命令。")
    return True


def handle_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    sender_info = data.event.sender
    sender = getattr(getattr(sender_info, "sender_id", None), "open_id", "") or ""
    sender_type = getattr(sender_info, "sender_type", "") or ""
    chat_type = msg.chat_type
    msg_id = msg.message_id or ""
    event_id = getattr(getattr(data, "header", None), "event_id", "") or ""
    fingerprint = _event_fingerprint(data)
    event_hash = hashlib.sha256(
        (event_id or msg_id or fingerprint).encode()
    ).hexdigest()[:10]

    # Persist before any reply or execution. Fail closed if state cannot be saved.
    try:
        is_new = _mark_seen(msg_id, event_id, fingerprint)
    except Exception as exc:  # noqa: BLE001
        logging.error(
            "seen state persist failed; event ignored hash=%s error=%s", event_hash, exc
        )
        return
    if not is_new:
        logging.info("dup event ignored hash=%s", event_hash)
        return

    stale_reason = _stale_event_reason(data)
    if stale_reason is not None:
        logging.warning(
            "stale event ignored hash=%s reason=%s", event_hash, stale_reason
        )
        return

    # Only human user messages may reach the command path. Never reply to rejected
    # senders, otherwise bot/app messages can create a feedback loop.
    if sender_type != "user":
        logging.warning(
            "non-user event ignored hash=%s sender_type=%s",
            event_hash,
            sender_type or "?",
        )
        return
    if sender not in ALLOW_OPEN_IDS:
        # sender 记入日志便于首次部署时收集本机用户 open_id（隐私：仅 open_id）
        logging.warning(
            "unauthorized event ignored hash=%s sender=%s", event_hash, sender
        )
        return
    logging.info("accepted event hash=%s chat_type=%s", event_hash, chat_type)

    # 群聊：只响应 @ 了机器人的消息
    if chat_type == "group":
        mentioned = {
            getattr(getattr(mention, "id", None), "open_id", "")
            for mention in (msg.mentions or [])
        }
        if BOT_OPEN_ID not in mentioned:
            return
    # 仅处理文本消息
    if msg.message_type != "text":
        return
    try:
        text = _strip_mention(data, json.loads(msg.content).get("text", ""))
    except Exception:  # noqa: BLE001
        return
    if not text:
        return
    # 日志脱敏（审计 H4）：不记录 prompt 原文
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    logging.info("prompt len=%d hash=%s", len(text), digest)

    if _handle_command(data, text, msg.chat_id or ""):
        return

    # 耗时执行必须放后台线程：WS 事件循环是单线程，同步阻塞会让 ping
    # 超时被服务器断连（3003 ping_timeout）
    threading.Thread(
        target=_execute_and_reply,
        args=(data, text, msg.chat_id or ""),
        daemon=True,
    ).start()


def _execute_and_reply(
    data: P2ImMessageReceiveV1, text: str, chat_id: str = "", *, queued: bool = False
) -> None:
    if queued:
        # 排队项：直接等锁（不抢跑也不回"忙"），轮到自己就执行
        _lock.acquire()
    elif not _lock.acquire(blocking=False):
        # 执行中：打断词直接终止进程组（不经过 _lock），其余消息回"忙"提示
        if _is_interrupt(text):
            if _request_interrupt():
                _send_reply(data, "🛑 已打断当前执行。")
            else:
                _send_reply(data, "（当前没有正在执行的指令）")
        elif not _send_reply(
            data, "⏳ 上一条指令还在执行。回复「停」可打断；想接续干活用 /queue <指令>。"
        ):
            logging.warning("busy reply send failed")
        return
    try:
        if _restart_pending.is_set():
            _send_reply(data, "🔄 bridge 正在重启，就绪后再发送指令。")
            return
        selected = sessions.harness_name(chat_id)
        if selected == "dsh" and not harness.REGISTRY["dsh"].available():
            _send_reply(data, "❌ 本机没有找到 dsh，安装后重启服务；可用 /claude 切回。")
            return
        start = time.time()
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        logging.info("exec start hash=%s", digest)
        # 立刻发一张卡片：既作"已收到"回执，也作为后续原地更新的载体。
        # 若等到第一个进度节流窗口才创建，短任务/慢启动会有 20s 无反馈。
        card_id: list[str | None] = [
            _post_card_reply(data, _card(RunState(selected), 0.0, starting=True))
        ]
        card_dead: list[bool] = [card_id[0] is None]
        if card_dead[0]:
            _send_reply(data, "🔄 收到，正在执行…（进度卡片不可用，完成后回复）")

        def _on_progress(state: RunState, elapsed_s: float) -> None:
            if STREAM_INTERVAL <= 0 or card_dead[0] or card_id[0] is None:
                return
            tick = time.monotonic()
            ok = _patch_card(card_id[0], _card(state, elapsed_s))
            # 记下每次刷新的耗时：刷新看着"变慢"时，要能区分是节流没触发、
            # 单次 PATCH 变慢，还是服务端限频（后者会静默不生效）。
            logging.info(
                "card tick at %.0fs patch=%.2fs ok=%s", elapsed_s, time.monotonic() - tick, ok
            )
            if not ok:
                card_dead[0] = True

        runner = _run_dsh if selected == "dsh" else _run_claude
        answer, state = runner(text, chat_id, on_progress=_on_progress)
        elapsed = time.time() - start
        # 打断时用已产出的部分文本，而不是那句 rc 报错（结局由卡片标题表达，
        # 正文不再重复"已打断"）。
        if state.interrupted:
            answer = state.answer().strip() or "（打断时还没有产出内容）"
        logging.info(
            "exec done in %.0fs card=%s interrupted=%s", elapsed, bool(card_id[0]), state.interrupted
        )
        # 收尾一律用卡片：短任务（< 节流窗口）没有进度回调，此前会退化成纯文本。
        done_card = _card(state, elapsed, done=answer)
        delivered = False
        if card_id[0] is not None and not card_dead[0]:
            delivered = _patch_card(card_id[0], done_card)
        elif not card_dead[0]:
            delivered = _post_card_reply(data, done_card) is not None
        if not delivered:
            head = done_card["header"]["title"]["content"]  # 标题已含耗时
            prefix = f"{head}\n{state.statusline()}\n\n"
            if not _send_reply(data, f"{prefix}{answer}"):
                logging.warning("result reply send failed")
        elif len(answer) > CARD_BODY_MAX:
            # 卡片放不下的部分接着发（旧行为是整段丢弃，长报告会缺内容）
            if not _send_reply(data, f"（接上条卡片）\n{answer[CARD_BODY_MAX:]}"):
                logging.warning("overflow reply send failed")
        sessions.update_stats(chat_id, state, elapsed)
    except subprocess.TimeoutExpired as exc:
        logging.error("exec timeout >%ss", exc.timeout)
        _send_reply(data, f"⏰ {selected} 执行超时（>{exc.timeout}s），进程组已终止。")
    except Exception as exc:  # noqa: BLE001
        logging.error("exec error: %s", exc)
        _send_reply(data, f"💥 内部错误：{exc}")
    finally:
        _lock.release()
        _kick_queue()  # 任意 chat 有积压都由唯一消费者接手；空队列时是空操作


class ObservedClient(ws.Client):
    async def _connect(self) -> None:
        # Keep SDK logging at WARNING: its INFO log includes the signed WS URL.
        await super()._connect()
        if self._conn is not None:
            logging.info("Feishu websocket connected")
            if _service_ready is not None:
                _service_ready()


def main() -> int:
    # 审计 C3：fail-closed——白名单为空且未显式开启开放模式时拒绝启动
    if not APP_ID or not APP_SECRET:
        print("FEISHU_APP_ID / FEISHU_APP_SECRET 未设置", flush=True)
        return 1
    if not ALLOW_OPEN_IDS and not OPEN_MODE:
        print(
            "FEISHU_ALLOW_OPEN_IDS 为空且未设置 FEISHU_OPEN_MODE=1，拒绝启动（安全默认）",
            flush=True,
        )
        return 1
    if not pathlib.Path(SETTINGS).is_file():
        print(f"遥控专用 settings 不存在: {SETTINGS}", flush=True)
        return 1
    event_handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )
    ws_client = ObservedClient(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=LogLevel.WARNING,
    )
    logging.info(
        "bridge started (bot_configured=%s, allow=%d, open_mode=%s)",
        bool(BOT_OPEN_ID),
        len(ALLOW_OPEN_IDS),
        OPEN_MODE,
    )
    ws_client.start()  # 长连接，阻塞
    return 0


if __name__ == "__main__":
    sys.exit(main())
