"""
Agent Loop Guard — 防止 AI 工具调用进入无限循环。
来源: OpenJarvis (https://github.com/open-jarvis/OpenJarvis)
适配：去掉 Rust 依赖，纯 Python 实现。
"""
from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass


@dataclass
class LoopVerdict:
    blocked: bool = False
    reason: str = ""
    warned: bool = False


class LoopGuard:
    """
    检测并阻止退化的 Agent 工具调用循环。

    功能：
    1. 相同调用追踪: 同一 (工具名, 参数) 超过 max_identical_calls 次 → 阻断
    2. 乒乓检测: 滑动窗口检测 A-B-A-B 或 A-B-C-A-B-C 模式
    3. 单工具预算: 同一工具调用超过 poll_budget 次 → 阻断
    4. 首次警告机制: 第一次触发只警告不阻断
    """

    def __init__(
        self,
        max_identical_calls: int = 3,
        ping_pong_window: int = 6,
        poll_tool_budget: int = 5,
        warn_before_block: bool = True,
    ):
        self._max_identical = max_identical_calls
        self._ping_pong_window = ping_pong_window
        self._poll_budget = poll_tool_budget
        self._warn_before_block = warn_before_block

        self._call_counts: dict[str, int] = {}
        self._tool_sequence: deque[str] = deque(maxlen=ping_pong_window * 2)
        self._per_tool_counts: dict[str, int] = {}
        self._warned_cycles: set[str] = set()

    def check_call(self, tool_name: str, arguments: str) -> LoopVerdict:
        verdict = self._check(tool_name, arguments)
        if verdict.blocked and self._warn_before_block:
            key = verdict.reason
            if key not in self._warned_cycles:
                self._warned_cycles.add(key)
                return LoopVerdict(blocked=False, warned=True, reason=key)
        return verdict

    def _check(self, tool_name: str, arguments: str) -> LoopVerdict:
        # 1. 相同调用检测
        call_hash = hashlib.sha256(f"{tool_name}:{arguments}".encode()).hexdigest()[:16]
        self._call_counts[call_hash] = self._call_counts.get(call_hash, 0) + 1
        if self._call_counts[call_hash] > self._max_identical:
            return LoopVerdict(
                blocked=True,
                reason=f"工具 '{tool_name}' 相同参数调用 {self._call_counts[call_hash]} 次",
            )

        # 2. 单工具预算
        self._per_tool_counts[tool_name] = self._per_tool_counts.get(tool_name, 0) + 1
        if self._per_tool_counts[tool_name] > self._poll_budget:
            return LoopVerdict(
                blocked=True,
                reason=f"工具 '{tool_name}' 超过调用预算 ({self._poll_budget})",
            )

        # 3. 乒乓检测
        self._tool_sequence.append(tool_name)
        if len(self._tool_sequence) >= self._ping_pong_window:
            if self._detect_ping_pong():
                return LoopVerdict(blocked=True, reason="检测到重复工具调用模式")

        return LoopVerdict()

    def _detect_ping_pong(self) -> bool:
        seq = list(self._tool_sequence)
        for period in (2, 3):
            if len(seq) >= period * 2:
                tail = seq[-period * 2:]
                pattern = tail[:period]
                if all(tail[i] == pattern[i % period] for i in range(len(tail))):
                    return True
        return False

    def reset(self):
        self._call_counts.clear()
        self._tool_sequence.clear()
        self._per_tool_counts.clear()
        self._warned_cycles.clear()
