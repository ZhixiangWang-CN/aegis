"""
邮件指令注入防护
基于 OpenJarvis injection_scanner.py 的正则模式，纯 Python 实现。

防止攻击者在邮件正文中注入恶意指令，劫持Aegis的命令执行。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List


class ThreatLevel(str, Enum):
    CLEAN    = "clean"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


@dataclass
class ScanFinding:
    pattern_name: str
    threat_level: ThreatLevel
    description:  str
    matched_text: str


@dataclass
class ScanResult:
    is_clean:     bool
    threat_level: ThreatLevel
    findings:     List[ScanFinding]

    def __str__(self) -> str:
        if self.is_clean:
            return "✅ 无注入风险"
        return (f"⚠️ 检测到 {len(self.findings)} 个风险 "
                f"[{self.threat_level.value.upper()}]: "
                + "; ".join(f.description for f in self.findings[:3]))


# ── 注入模式（直接来自 OpenJarvis injection_scanner.py）──────────────

_PATTERNS = [
    # 覆盖系统指令
    (r"(?i)ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)",
     "prompt_override", ThreatLevel.HIGH, "尝试覆盖系统指令"),
    (r"(?i)disregard\s+(?:all\s+)?(?:previous|prior|your)\s+(?:instructions?|programming|rules?)",
     "prompt_override", ThreatLevel.HIGH, "尝试忽略系统规则"),
    (r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:different|new|my)",
     "identity_override", ThreatLevel.HIGH, "尝试改变AI身份"),

    # 代码/命令注入
    (r"(?:;|\||&&)\s*(?:rm|curl|wget|nc|ncat|bash|sh|python|perl|powershell)\s",
     "shell_injection", ThreatLevel.CRITICAL, "Shell命令注入"),
    (r"(?i)(?:execute|run|eval)\s*\(\s*['\"]",
     "code_injection", ThreatLevel.HIGH, "代码执行尝试"),

    # 数据外泄
    (r"(?i)(?:send|post|upload|exfiltrate|transmit)\s+(?:.*\s+)?(?:to\s+)?https?://",
     "exfiltration", ThreatLevel.HIGH, "数据外泄尝试"),

    # Jailbreak
    (r"(?i)(?:DAN|do\s+anything\s+now)\s+(?:mode|prompt|jailbreak)",
     "jailbreak", ThreatLevel.HIGH, "DAN越狱尝试"),
    (r"(?i)pretend\s+(?:you\s+)?(?:have\s+)?no\s+(?:restrictions?|limitations?|rules?)",
     "jailbreak", ThreatLevel.MEDIUM, "绕过限制尝试"),

    # 角色分隔符注入
    (r"```(?:system|assistant)\b",
     "delimiter_injection", ThreatLevel.MEDIUM, "角色分隔符注入"),
    (r"<\|(?:im_start|im_end|system|assistant)\|>",
     "delimiter_injection", ThreatLevel.HIGH, "聊天模板注入"),

    # Aegis特定防护：伪造系统邮件
    (r"(?i)Aegis.*(?:执行|运行|删除|发送|转账|密码)",
     "jarvis_impersonation", ThreatLevel.HIGH, "伪造Aegis系统指令"),
    (r"(?i)(?:system|admin|root|jarvis)\s*[:：]\s*(?:execute|run|cmd|bash)",
     "jarvis_impersonation", ThreatLevel.HIGH, "管理员权限伪造"),
]

_COMPILED = [
    (re.compile(pat), name, level, desc)
    for pat, name, level, desc in _PATTERNS
]

_LEVEL_ORDER = [ThreatLevel.CLEAN, ThreatLevel.LOW,
                ThreatLevel.MEDIUM, ThreatLevel.HIGH, ThreatLevel.CRITICAL]


class InjectionScanner:
    """扫描文本中的提示词注入攻击模式"""

    def scan(self, text: str) -> ScanResult:
        findings = []
        max_level = ThreatLevel.CLEAN

        for pattern, name, level, desc in _COMPILED:
            match = pattern.search(text)
            if match:
                findings.append(ScanFinding(
                    pattern_name=name,
                    threat_level=level,
                    description=desc,
                    matched_text=match.group(0)[:80],
                ))
                if _LEVEL_ORDER.index(level) > _LEVEL_ORDER.index(max_level):
                    max_level = level

        return ScanResult(
            is_clean=len(findings) == 0,
            threat_level=max_level,
            findings=findings,
        )


# 全局单例
_scanner = InjectionScanner()


def scan(text: str) -> ScanResult:
    return _scanner.scan(text)


def is_safe(text: str, max_level: ThreatLevel = ThreatLevel.MEDIUM) -> bool:
    """
    检查文本是否安全（低于指定风险级别）。
    默认允许 LOW 级别，拒绝 MEDIUM 及以上。
    """
    result = scan(text)
    return _LEVEL_ORDER.index(result.threat_level) < _LEVEL_ORDER.index(max_level)
