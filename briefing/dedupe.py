"""30 天查重表 state/covered.csv：记录每期讲过的内容，写稿前过滤素材，写完再查一次。"""
import csv
import datetime as dt
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLE = os.path.join(ROOT, "state", "covered.csv")
FIELDS = ["date", "slot", "section", "title", "source"]
DAYS = 30
THRESHOLD = 0.5


def load(today=None, days=DAYS):
    if not os.path.exists(TABLE):
        return []
    today = dt.date.fromisoformat(today) if today else dt.date.today()
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    with open(TABLE, encoding="utf-8", newline="") as f:
        return [r for r in csv.DictReader(f) if r["date"] >= cutoff]


def append(rows):
    new = not os.path.exists(TABLE)
    with open(TABLE, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS)
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


def fresh(items, rows):
    """去掉 30 天内已讲过的素材。"""
    return [it for it in items if not is_covered(it["title"], rows)]


def titles(rows):
    return list(dict.fromkeys(r["title"] for r in rows))
