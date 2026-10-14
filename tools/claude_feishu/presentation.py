"""Observe Agent event streams and render cards without network or disk I/O."""

from __future__ import annotations

import json
import os
import pathlib

from tools.claude_feishu import harness

THINKING_MAX = 200
STEPS_MAX = 40
STEPS_SHOWN = 5
OUTPUT_MAX = 320
CARD_BODY_MAX = 3500


def short_path(path: str, home_dir: pathlib.Path | None = None) -> str:
    """家目录缩成 ~，标题栏里省地方。"""
    home = str(home_dir or pathlib.Path.home())
    return "~" + path[len(home) :] if path.startswith(home) else path


def _clip(text: str, limit: int) -> str:
    """压掉换行/多空格并截断——卡片一行放不下长文本。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _fmt_duration(elapsed: float) -> str:
    """执行时长的紧凑写法：min+s 两个单位（2min13s / 45s）。"""
    mins, secs = divmod(int(elapsed), 60)
    return f"{mins}min{secs:02d}s" if mins else f"{secs}s"


def _clip_tail(text: str, limit: int) -> str:
    """保留末尾——流式正文/长输出里，最新的部分才是有信息量的。"""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# 工具调用摘要里每种工具取哪个入参字段（其余字段对"在干什么"没信息量）
_TOOL_DETAIL_KEYS = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
    "Grep": ("pattern",),
    "Glob": ("pattern",),
    "Task": ("description",),
    "Agent": ("description",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Skill": ("skill",),
}
_TOOL_DETAIL_MAX = 120


def _tool_summary(part: dict) -> str:
    """工具调用的一行摘要：Bash 带命令、Read/Edit 带路径、Grep 带模式。

    只写工具名的话卡片上永远只有"调用 Bash"，看不出在干什么。
    """
    name = str(part.get("name") or "?")
    data = part.get("input") if isinstance(part.get("input"), dict) else {}
    detail = ""
    for key in _TOOL_DETAIL_KEYS.get(name, ()):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            detail = value
            break
    if not detail and data:
        detail = json.dumps(data, ensure_ascii=False)
    return f"{name}: {_clip(detail, _TOOL_DETAIL_MAX)}" if detail else name


class RunState:
    """执行期的可观测状态：模型、思考、当前动作、步骤历史、轮次与用量。

    来源是 claude 的 stream-json 事件；全部字段都是"有则更新"，缺事件不影响
    最终结果——最终回答始终以 result 事件（或累积文本兜底）为准。
    """

    def __init__(
        self,
        harness_name: str = "claude",
        *,
        context_window: int = 0,
        default_workdir: str = "",
        home_dir: pathlib.Path | None = None,
        effort: str = "",
        claude_model: str = "",
    ) -> None:
        self.harness = harness_name
        self.session_id = ""
        self.cwd = ""
        self.step = 0
        self.initial_turn: int | None = None
        self.context_window = context_window
        self.default_workdir = default_workdir
        self.home_dir = home_dir
        self.effort = effort
        self.claude_model = claude_model
        self.events_truncated = False
        self.stream_error = ""
        self.terminal_reason = ""
        self.model = ""
        self.workdir = ""  # 本次执行的工作目录（/cd 切换后与默认值不同）
        self.thinking = 0
        self.thinking_text = ""  # 最新一段思考正文（thinking 块）
        self.live_thinking = ""  # 正在流式生成的思考（partial messages）
        self.live_text = ""  # 正在流式生成的正文（partial messages）
        self.last_result = ""  # 最近一次工具输出（命令输出/报错）
        self.last_result_error = False
        self.last_output = ""  # "text" | "result"：最新输出区显示哪个
        self.action = ""
        self.steps: list[str] = []  # 滚动步骤历史（工具调用一行摘要）
        self.turns = 0
        self.usage: dict = {}  # 最后一次 assistant 调用的用量（= 当前上下文长度）
        self.total_usage: dict = {}  # result 事件的累计用量（整轮所有调用之和）
        self.cost: float | None = None
        self.result: str | None = None
        self.text_parts: list[str] = []
        self.returncode: int | None = None
        self.interrupt_requested = False

    def observe(self, event: dict) -> None:
        harness.REGISTRY[self.harness].observe(self, event)

    def add_step(self, action: str) -> None:
        self.action = action
        self.steps.append(action)
        del self.steps[:-STEPS_MAX]

    def _observe_claude(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system":
            if event.get("subtype") == "init":
                self.model = str(event.get("model") or self.model)
            elif event.get("subtype") == "thinking_tokens":
                self.thinking = max(
                    self.thinking, int(event.get("estimated_tokens") or 0)
                )
        elif kind == "stream_event":
            self._observe_stream(event.get("event") or {})
        elif kind == "user":
            self._observe_tool_result(event)
        elif kind == "assistant":
            self.turns += 1
            msg = event.get("message") or {}
            # 用量随每条 assistant 事件下发；result 事件要到整轮结束才有，
            # 只认 result 的话执行中的卡片永远显示不出上下文占比。
            if isinstance(msg.get("usage"), dict):
                self.usage = msg["usage"]
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "thinking":
                    # 思考正文只留最新一段；空块（redacted）不覆盖上一段。
                    text = str(part.get("thinking") or "").strip()
                    if text:
                        self.thinking_text = text
                elif part.get("type") == "text" and part.get("text"):
                    self.text_parts.append(str(part["text"]))
                    self.action = str(part["text"]).strip().splitlines()[0][:120]
                elif part.get("type") == "tool_use":
                    self.action = _tool_summary(part)
                    self.steps.append(self.action)
                    del self.steps[:-STEPS_MAX]
        elif kind == "result":
            self.result = (
                event.get("result")
                if isinstance(event.get("result"), str)
                else self.result
            )
            # result 的 usage 是整轮所有 API 调用的累计值（实测 = 各次 assistant
            # 之和），不是上下文长度——用它算 ctx 会随调用次数虚高（同一会话里
            # 3 次调用就 3 倍）。所以只留作统计，占比始终以最后一次 assistant
            # 事件的 usage 为准。
            if isinstance(event.get("usage"), dict):
                self.total_usage = event["usage"]
            if isinstance(event.get("total_cost_usd"), (int, float)):
                self.cost = float(event["total_cost_usd"])
            if isinstance(event.get("num_turns"), int):
                self.turns = max(self.turns, int(event["num_turns"]))

    def _observe_stream(self, ev: dict) -> None:
        """partial message 事件（--include-partial-messages）：逐字累积正文/思考。

        有了这个才能在卡片上看到"正在写的字"，而不是每 5 秒才跳一次的快照。
        """
        etype = ev.get("type")
        if etype == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "text":
                self.live_text = ""
            elif block.get("type") == "thinking":
                self.live_thinking = ""
        elif etype == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                self.live_text += str(delta.get("text") or "")
                self.last_output = "text"
            elif dtype == "thinking_delta":
                self.live_thinking += str(delta.get("thinking") or "")

    def _observe_tool_result(self, event: dict) -> None:
        """user 事件里的 tool_result：终端里最常看的东西（命令输出/报错）。"""
        msg = event.get("message") or {}
        for part in msg.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            content = part.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = str(content or "")
            text = text.strip()
            if not text:
                continue
            self.last_result = text
            self.last_result_error = bool(part.get("is_error"))
            self.last_output = "result"

    def latest_output(self) -> str:
        """终端里最常盯的那块：正在写的正文，或最近一次工具输出。"""
        if self.last_output == "text" and self.live_text.strip():
            return _clip_tail(self.live_text, OUTPUT_MAX)
        if self.harness == "dsh" and self.last_output == "result":
            return self.last_result[:OUTPUT_MAX] + (
                "…" if len(self.last_result) > OUTPUT_MAX else ""
            )
        if self.last_output == "result" and self.last_result:
            return _clip(self.last_result, OUTPUT_MAX)
        if self.live_text.strip():
            return _clip_tail(self.live_text, OUTPUT_MAX)
        return _clip(self.last_result, OUTPUT_MAX) if self.last_result else ""

    def answer(self) -> str:
        if self.result is not None:
            return self.result
        if self.text_parts:
            return "\n".join(self.text_parts).strip()
        return self.live_text.strip()  # 只有 partial 事件时兜底

    @property
    def interrupted(self) -> bool:
        """本轮是否被 _request_interrupt 打断。

        claude CLI 抓到 SIGTERM 后是自己退出的，rc=143（128+15）而不是 -15，
        只认负信号值会漏判（实测日志里请求了打断却记成 interrupted=False），
        所以以显式标记为准、信号值兜底。
        """
        if self.interrupt_requested:
            return True
        return harness.REGISTRY[self.harness].is_interrupt(self.returncode)

    def progress_md(self, elapsed: float) -> str:
        """进度卡片正文：统计 + 思考摘要 + 当前动作 + 最近步骤。

        耗时在卡片主标题、模型/effort/路径/上下文占比在副标题，正文都不重复。
        手机上要短：思考取最新一段的前 THINKING_MAX 字，历史步骤只列最近
        STEPS_SHOWN 条。
        """
        stats = []
        if self.step:
            stats.append(f"步骤 {self.step}")
        if self.turns:
            stats.append(f"轮次 {self.turns}")
        if self.thinking >= 1000:
            stats.append(f"思考 {self.thinking / 1000:.1f}k")
        elif self.thinking:
            stats.append(f"思考 {self.thinking}")
        lines = [" · ".join(stats)] if stats else []
        # 思考：优先流式缓冲（正在写的那段），其次 assistant 事件里的完整块
        thinking = self.live_thinking.strip() or self.thinking_text
        if thinking:
            lines.append(f"💭 {_clip(thinking, THINKING_MAX)}")
        if self.action:
            lines.append(f"- 当前：{self.action}")
        output = self.latest_output()
        if output:
            # 工具输出保留换行（命令回显/报错按行读更清楚），正文压平
            body = output if self.last_output == "result" else " ".join(output.split())
            mark = (
                "⚠️ " if self.last_result_error and self.last_output == "result" else ""
            )
            lines.append("**最新输出**")
            lines.append(f"{mark}{body}")
        # 当前动作若是工具调用，已在"当前"行显示，历史里不再重复最后一条
        history = (
            self.steps[:-1]
            if self.steps and self.steps[-1] == self.action
            else self.steps
        )
        history = history[-STEPS_SHOWN:]
        if self.events_truncated:
            lines.append("部分事件已截断，用量可能不完整。")
        if history:
            lines.append("**最近步骤**")
            lines.extend(f"{i}. {s}" for i, s in enumerate(history, 1))
        return "\n".join(lines)

    def context_used(self) -> int:
        """本轮上下文占用 = 本轮送进模型的全部 token。

        含未命中缓存的新输入 + 命中缓存的复用部分 + 本轮新建缓存；三者之和
        才是这次请求实际占用的上下文长度。
        """
        return sum(
            int(self.usage.get(key) or 0)
            for key in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        )

    def _ctx_text(self) -> str:
        used = self.context_used()
        if not used:
            return ""
        if not self.context_window:
            return f"ctx {used / 1000:.0f}k"  # 不知窗口就只报绝对量，不算百分比
        return f"ctx {used / self.context_window * 100:.0f}%"

    def model_text(self) -> str:
        """Configured display name; DSH does not report a model in its stream."""
        configured = os.environ.get(
            f"CLAUDE_FEISHU_{self.harness.upper()}_MODEL_NAME", ""
        ).strip()
        if configured:
            return configured
        if self.harness == "claude":
            return self.model or self.claude_model or "模型未知"
        return "模型未配置"

    def location_text(self) -> str:
        return f"{short_path(self.workdir or self.default_workdir, self.home_dir)} · {self._ctx_text() or 'ctx 未知'}"

    def statusline(self) -> str:
        """Text reply fallback mirrors the card subtitle: model · 目录 · ctx，单行。"""
        model = self.model_text()
        if self.harness == "claude":
            model = f"{model} {self.effort}".strip()
        return f"{model} · {self.location_text()}"


def _usage_note(state: RunState) -> str:
    """收尾卡脚注：轮次 / 输出 token / 成本（终端里 /cost 给的信息）。"""
    bits = []
    if state.turns:
        bits.append(f"{state.turns} 轮")
    out_tokens = int((state.total_usage or {}).get("output_tokens") or 0)
    if out_tokens >= 1000:
        bits.append(f"输出 {out_tokens / 1000:.1f}k tok")
    elif out_tokens:
        bits.append(f"输出 {out_tokens} tok")
    if state.cost is not None:
        bits.append(f"${state.cost:.4f}")
    if state.events_truncated:
        bits.append("部分事件已截断，用量可能不完整")
    return " · ".join(bits)


def card(
    state: RunState,
    elapsed: float,
    *,
    done: str | None = None,
    starting: bool = False,
    subtitle_supported: bool = True,
) -> dict:
    """构建进度/结果卡片。done 非空表示收尾态。

    主标题 = harness + 状态 + 耗时；副标题 = 模型 · 目录 · 上下文（飞书 header 只有
    title + subtitle 两行，三样信息必须合成一行副标题）。
    进度与收尾两行布局一致，不随正文长度改变。
    标题按实际结局着色，打断/失败不能顶着绿色的"执行完成"。
    """
    label = "Claude" if state.harness == "claude" else "DSH"
    if done is None:
        title, template = f"🤖 {label} 执行中 · {_fmt_duration(elapsed)}", "blue"
        body = "已接收，正在启动…" if starting else state.progress_md(elapsed)
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]
    else:
        if state.interrupted:
            title, template = f"🛑 {label} 已打断", "orange"
        elif state.returncode not in (0, None):
            title, template = f"⚠️ {label} 执行失败 (rc={state.returncode})", "red"
        else:
            title, template = f"✅ {label} 执行完成", "green"
        if (
            state.harness == "dsh"
            and state.returncode not in (0, None)
            and not state.interrupted
            and state.terminal_reason
        ):
            title = f"⚠️ {label} 执行失败 ({state.terminal_reason})"
        title += f" · {_fmt_duration(elapsed)}"  # 耗时进主标题，正文里不再重复
        body = (
            done
            if len(done) <= CARD_BODY_MAX
            else done[:CARD_BODY_MAX] + "\n…（正文过长，剩余部分随后发出）"
        )
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]
        note = _usage_note(state)
        if note:
            elements.append(
                {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]}
            )
    header = {
        "template": template,
        "title": {"tag": "plain_text", "content": title},
    }
    if subtitle_supported:
        header["subtitle"] = {
            "tag": "plain_text",
            "content": f"{state.model_text()} · {state.location_text()}",
        }
    else:
        # 服务端不认副标题时，模型与目录/上下文落到正文首两行，信息不丢
        elements.insert(
            0,
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": state.location_text()},
            },
        )
        elements.insert(
            0,
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": state.model_text()},
            },
        )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "elements": elements,
    }
