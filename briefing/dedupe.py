"""30 天查重表 state/covered.csv：记录每期讲过的内容，写稿前过滤素材，写完再查一次。

两层查重：
1. 标题相似度（同一条新闻不重复）；
2. 话题（topic）：同一话题（如利率、房租）近 HARD_DAYS 天讲过就直接从素材里剔除，
   30 天内讲过的只有出现官方决定 / 数据发布这类实质新事件才允许再讲。
"""
import csv
import datetime as dt
import os
import re
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = os.path.join(ROOT, "state", "covered.csv")
FIELDS = ["date", "slot", "section", "title", "source", "topic"]
DAYS = 30        # 软限制：30 天内讲过的话题，没有实质新事件不再讲
HARD_DAYS = 7    # 硬限制：7 天内讲过的话题，相关素材直接剔除（官方决定/数据发布除外）
THRESHOLD = 0.5

# 话题表：写稿时让模型从中选一个；旧记录没有 topic 时按关键词自动归类。
# 顺序有意义：先匹配更具体的话题。
TOPICS = OrderedDict([
    ("美联储与美国经济", [r"美联储", r"\bFed\b", r"Federal Reserve", r"Powell", r"鲍威尔", r"美国(经济|通胀|就业|CPI)"]),
    ("澳洲利率与央行", [r"利率", r"加息", r"降息", r"央行", r"\bRBA\b", r"Reserve Bank", r"cash rate", r"interest rate",
                    r"rate (hike|cut|rise)", r"Bullock", r"布洛克", r"房贷利率", r"mortgage rate"]),
    ("通胀与生活成本", [r"通胀", r"\bCPI\b", r"inflation", r"生活成本", r"cost of living", r"物价"]),
    ("房租房价", [r"房租", r"租金", r"房价", r"租房", r"楼市", r"房地产市场", r"\brent(s|al)?\b", r"house price",
               r"home price", r"property (market|price)", r"housing (market|crisis|affordability)"]),
    ("移民签证", [r"移民", r"签证", r"\bvisa", r"migration", r"migrant", r"永居"]),
    ("就业与工资", [r"失业", r"就业", r"工资", r"unemployment", r"\bjobs? (data|figures|report)", r"wage"]),
    ("汇率", [r"汇率", r"澳元", r"人民币", r"\bAUD\b", r"exchange rate"]),
    ("中澳关系", [r"中澳", r"China[- ]Australia", r"Australia[- ]China"]),
    ("油价与大宗商品", [r"油价", r"原油", r"oil price", r"铁矿石", r"iron ore", r"大宗商品"]),
])
OTHER = "其他"

# 官方决定 / 数据发布：即使话题刚讲过也保留素材，让模型判断是否构成实质新事件
HARD_EVENT = re.compile(
    r"\b(decision|decides|announce[sd]?|releases?d?|data shows|figures show|passe[sd]|unveil(s|ed)?)\b|"
    r"宣布|公布|发布|通过|决议|决定", re.I)


def classify(text):
    for topic, pats in TOPICS.items():
        if any(re.search(p, text or "", re.I) for p in pats):
            return topic
    return OTHER


def _migrate():
    """旧表没有 topic 列：补列并按关键词回填。"""
    if not os.path.exists(TABLE):
        return
    with open(TABLE, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames and "topic" in reader.fieldnames:
            return
        rows = list(reader)
    for r in rows:
        r["topic"] = classify(r.get("title", "") + " " + r.get("source", ""))
    with open(TABLE, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def load(today=None, days=DAYS):
    _migrate()
    if not os.path.exists(TABLE):
        return []
    today = dt.date.fromisoformat(today) if today else dt.date.today()
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    with open(TABLE, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["date"] >= cutoff]
    for r in rows:
        if not r.get("topic"):
            r["topic"] = classify(r.get("title", "") + " " + r.get("source", ""))
    return rows


def append(rows):
    _migrate()
    new = not os.path.exists(TABLE)
    with open(TABLE, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def _grams(s):
    s = re.sub(r"[^\w]", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar(a, b):
    x, y = _grams(a), _grams(b)
    return bool(x and y) and len(x & y) / len(x | y) >= THRESHOLD


def is_covered(title, rows):
    return any(similar(title, r["title"]) or (r.get("source") and similar(title, r["source"])) for r in rows)


def topic_stats(rows):
    """{话题: (次数, 最近日期)}，不含「其他」。"""
    stats = {}
    for r in rows:
        t = r.get("topic") or OTHER
        if t == OTHER:
            continue
        n, last = stats.get(t, (0, ""))
        stats[t] = (n + 1, max(last, r["date"]))
    return stats


def hot_topics(rows, today, days=HARD_DAYS):
    """近 days 天讲过的话题。"""
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=days)).isoformat()
    return {t for t, (_, last) in topic_stats(rows).items() if last >= cutoff}


def fresh(items, rows, today=None):
    """去掉 30 天内讲过的同一条新闻，以及近 HARD_DAYS 天讲过的话题（官方决定/数据发布除外）。"""
    hot = hot_topics(rows, today or dt.date.today().isoformat())
    out = []
    for it in items:
        if is_covered(it["title"], rows):
            continue
        text = it["title"] + " " + it.get("desc", "")
        if classify(text) in hot and not HARD_EVENT.search(it["title"]):
            continue
        out.append(it)
    return out


def titles(rows):
    return list(dict.fromkeys(r["title"] for r in rows))


def topic_text(rows, today):
    stats = topic_stats(rows)
    hot = hot_topics(rows, today)
    if not stats:
        return "（暂无）"
    lines = []
    for t, (n, last) in sorted(stats.items(), key=lambda kv: kv[1][1], reverse=True):
        flag = ("【7 天内讲过：除非今天有官方决定或官方数据发布，否则禁止】" if t in hot
                else "【本月讲过：只有官方决定、数据发布或重大突发才可再讲】")
        lines.append(f"{t}：{n} 次，最近 {last} {flag}")
    return "\n".join(lines)
