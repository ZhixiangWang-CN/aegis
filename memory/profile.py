"""个人档案管理 — 持久化到 profile.json"""
import json
from datetime import datetime
import config

_DEFAULT = {
    "identity": {
        "email": config.NETEASE_EMAIL,
        "profession": "",
        "expertise": [],
        "current_goals": []
    },
    "communication_style": {
        "formal_contacts": [],
        "tone_preferences": {}
    },
    "behavior_patterns": {
        "frequent_topics": [],
        "decision_tendencies": []
    },
    "knowledge_domains": [],
    "raw_facts": [],          # 从文件/邮件里提取的碎片化事实
    "last_updated": ""
}


def load() -> dict:
    if config.PROFILE_PATH.exists():
        with open(config.PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return dict(_DEFAULT)


def save(profile: dict):
    profile["last_updated"] = datetime.now().isoformat()
    with open(config.PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)


def add_fact(fact: str):
    """追加一条关于用户的碎片事实"""
    profile = load()
    facts: list = profile.setdefault("raw_facts", [])
    if fact and fact not in facts:
        facts.append(fact)
        if len(facts) > 200:          # 最多保留200条
            facts.pop(0)
        save(profile)


def merge_extracted(info: dict):
    """将 ai.client.extract_profile_info 的结果合并进档案（双写：JSON + MEMORY.md）"""
    if not info:
        return

    # 同步到 MEMORY.md（主存储）
    try:
        from memory.memory_manage import merge_extracted as mm_merge
        mm_merge(info)
    except Exception:
        pass

    # 保持 JSON 备份
    p = load()

    def _merge_list(key, new_items):
        existing = p.get(key, [])
        for item in (new_items or []):
            if item and item not in existing:
                existing.append(item)
        p[key] = existing

    # profession 不自动覆盖，仅在手动设置时更新
    if info.get("profession") and not p["identity"].get("profession"):
        p["identity"]["profession"] = info["profession"]

    _merge_list("knowledge_domains", info.get("expertise"))

    goals = p["identity"].setdefault("current_goals", [])
    for g in (info.get("goals") or []):
        if g and g not in goals:
            goals.append(g)

    topics = p["behavior_patterns"].setdefault("frequent_topics", [])
    for t in (info.get("topics") or []):
        if t and t not in topics:
            topics.append(t)

    if info.get("insights"):
        add_fact(info["insights"])

    save(p)


def get_summary() -> str:
    """返回档案摘要字符串，用于注入 AI prompt（优先使用 MEMORY.md）"""
    try:
        from memory.memory_manage import get_summary as mm_summary
        summary = mm_summary()
        if summary and summary != "档案尚未建立":
            return summary
    except Exception:
        pass

    # 回退到 JSON
    p = load()
    parts = []
    if p["identity"].get("profession"):
        parts.append(f"职业/方向: {p['identity']['profession']}")
    if p["knowledge_domains"]:
        parts.append(f"专业领域: {', '.join(p['knowledge_domains'][:5])}")
    if p["identity"].get("current_goals"):
        parts.append(f"当前目标: {'; '.join(p['identity']['current_goals'][:3])}")
    if p["behavior_patterns"].get("frequent_topics"):
        parts.append(f"关注话题: {', '.join(p['behavior_patterns']['frequent_topics'][:6])}")
    if p.get("raw_facts"):
        parts.append(f"已知信息: {'; '.join(p['raw_facts'][-5:])}")
    return "\n".join(parts) if parts else "档案尚未建立"
