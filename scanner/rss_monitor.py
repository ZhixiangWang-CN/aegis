"""
RSS 学术订阅监控
基于 OpenJarvis news_rss.py，面向科研场景订阅期刊/预印本更新。

内置订阅源（可在 .credentials 中覆盖）:
  - PubMed 搜索结果（超声、放射组学、NAFLD等）
  - arXiv 医学图像处理
  - bioRxiv 肝脏疾病相关
  - 中国知网 RSS（如有）
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from email_module.reader import _safe_print
from memory import db, vector_store

FEEDS_PATH = config.DATA_DIR / "rss_feeds.json"

# 默认学术订阅源（贴合用户研究方向）
DEFAULT_FEEDS = [
    {
        "name": "PubMed - Radiomics",
        "url": "https://pubmed.ncbi.nlm.nih.gov/rss/search/1dXzCLGMgM7fZKXyxqAuL0yOTM/?limit=20&format=rss",
        "category": "academic",
        "tags": ["radiomics", "medical_imaging"],
    },
    {
        "name": "PubMed - NAFLD Ultrasound",
        "url": "https://pubmed.ncbi.nlm.nih.gov/rss/search/1Q2U7vPGmMlKlj_0pGfGLXANLEE/?limit=20&format=rss",
        "category": "academic",
        "tags": ["NAFLD", "ultrasound"],
    },
    {
        "name": "arXiv - Medical Image Analysis",
        "url": "https://arxiv.org/rss/eess.IV",
        "category": "preprint",
        "tags": ["deep_learning", "medical_imaging"],
    },
    {
        "name": "Nature - Liver Disease",
        "url": "https://www.nature.com/search.rss?q=liver+disease+AI&order=date_desc",
        "category": "academic",
        "tags": ["liver", "AI"],
    },
]


def _load_feeds() -> list[dict]:
    if FEEDS_PATH.exists():
        try:
            return json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return DEFAULT_FEEDS


def _save_feeds(feeds: list[dict]):
    FEEDS_PATH.write_text(json.dumps(feeds, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def add_feed(name: str, url: str, category: str = "academic",
             tags: list[str] | None = None):
    """添加自定义 RSS 订阅"""
    feeds = _load_feeds()
    feeds.append({"name": name, "url": url, "category": category,
                  "tags": tags or []})
    _save_feeds(feeds)
    _safe_print(f"[RSS] 已添加订阅: {name}")


def _fetch_feed(url: str) -> list[dict]:
    """拉取并解析单个 RSS/Atom feed"""
    try:
        import feedparser
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:20]:
            title   = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            link    = entry.get("link", "")
            pub     = entry.get("published", "") or entry.get("updated", "")
            uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()
            items.append({
                "id":      uid,
                "title":   title,
                "summary": summary[:500],
                "link":    link,
                "pub_date": pub,
            })
        return items
    except Exception as e:
        _safe_print(f"[RSS] 拉取失败 {url[:50]}: {e}")
        return []


def _ensure_rss_table():
    with db.get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rss_items (
                id TEXT PRIMARY KEY,
                feed_name TEXT,
                title TEXT,
                summary TEXT,
                link TEXT,
                pub_date TEXT,
                importance INTEGER DEFAULT 2,
                fetched_at TEXT
            )
        """)


def fetch_all_feeds(notify_important: bool = True) -> list[dict]:
    """
    拉取所有订阅源，存库，返回新文章列表。
    重要文章（含关键词）会推送邮件提醒。
    """
    _ensure_rss_table()
    feeds = _load_feeds()
    new_items = []
    now = datetime.now().isoformat()

    IMPORTANT_KEYWORDS = [
        "NAFLD", "NASH", "radiomics", "ultrasound", "liver fibrosis",
        "hepatic steatosis", "medical image", "deep learning",
        "超声", "放射组学", "肝纤维化", "肝脂肪变",
    ]

    for feed in feeds:
        items = _fetch_feed(feed["url"])
        saved = 0
        for item in items:
            with db.get_conn() as conn:
                existing = conn.execute(
                    "SELECT 1 FROM rss_items WHERE id=?", (item["id"],)
                ).fetchone()
                if existing:
                    continue

                # 判断重要性
                text_lower = (item["title"] + item["summary"]).lower()
                importance = 4 if any(k.lower() in text_lower
                                      for k in IMPORTANT_KEYWORDS) else 2

                conn.execute("""
                    INSERT OR IGNORE INTO rss_items
                    (id, feed_name, title, summary, link, pub_date, importance, fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (item["id"], feed["name"], item["title"], item["summary"],
                      item["link"], item["pub_date"], importance, now))
                saved += 1
                item["importance"] = importance
                item["feed_name"] = feed["name"]
                new_items.append(item)

            # 向量化
            try:
                vector_store.add_document(
                    collection_name="papers",
                    doc_id=item["id"],
                    text=f"{item['title']}\n{item['summary']}",
                    metadata={"source": feed["name"], "link": item["link"],
                              "importance": item.get("importance", 2)},
                )
            except Exception:
                pass

        if saved:
            _safe_print(f"[RSS] {feed['name']}: {saved} 篇新文章")

    # 推送重要文章
    if notify_important:
        important = [i for i in new_items if i.get("importance", 0) >= 4]
        if important:
            _notify_important_papers(important)

    return new_items


def _notify_important_papers(papers: list[dict]):
    """推送重要新论文到邮件"""
    from email_module.sender import send_email
    lines = ["Aegis学术雷达 — 发现与你研究相关的新论文:\n"]
    for p in papers[:10]:
        lines.append(
            f"★ 【{p['feed_name']}】{p['title']}\n"
            f"  {p['summary'][:150]}\n"
            f"  {p.get('link', '')}\n"
        )
    body = "\n".join(lines)
    send_email(config.NETEASE_EMAIL,
               f"📚 学术雷达: {len(papers)} 篇相关新论文", body)


def get_recent_papers(limit: int = 20, min_importance: int = 2) -> list[dict]:
    """获取近期重要论文"""
    _ensure_rss_table()
    with db.get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM rss_items
            WHERE importance >= ?
            ORDER BY fetched_at DESC
            LIMIT ?
        """, (min_importance, limit)).fetchall()
    return [dict(r) for r in rows]
