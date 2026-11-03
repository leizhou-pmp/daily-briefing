"""生成一期播报：选题 → 分栏目联网写稿 → 拼成脚本 → 更新去重历史。

用法: python -m briefing.generate --slot morning|evening [--date YYYY-MM-DD]
输出: out/<name>.md、out/<name>.txt（朗读稿）、out/meta.json
"""
import argparse
import datetime as dt
import json
import os
import re

from . import dedupe, gemini, news, topics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state", "history.json")
OUT = os.path.join(ROOT, "out")
BJT = dt.timezone(dt.timedelta(hours=8))
SLOT_CN = {"morning": "早报", "evening": "晚报"}
CHARS_PER_MIN = 330  # Yunxi +5% 语速实测约每分钟 330 字（含停顿）

STYLE = """【节目目的】一期节目同时解决四件事：帮听众融入澳洲生活、了解中国大事、了解国际大事、了解自己在悉尼从事的门窗幕墙扶手栏杆行业。只讲对这四件事有用的内容。

【播报风格：简洁、干货、新闻播报体】
- 像新闻主播，不像聊天：不寒暄、不铺垫、不自问自答，不用“好，我们来看”“说白了”“大家知道吗”“值得注意的是”这类过渡和口头禅。
- 听众就是“你”。不要写“对于在悉尼从事门窗行业的你”“对于即将移居澳洲的家庭来说”“作为……的从业者”这类身份铺垫，直接说“对你的影响：……”。
- 每条内容：事实（谁、什么、数字、时间）→ 原因（一句）→ 对你的影响（一句，具体可操作；没有直接影响就不说）。
- 信息密度优先：多给具体数字、名称、标准编号、价格、日期；删掉形容词、重复和空泛感慨。
- 不要念网址、不要用列表符号、不要用 Markdown；数字口语化（“百分之四点一”），英文专有名词首次出现给中文。
- 只讲有依据的事实；不确定的不说。
- 绝对不要编造听众公司（DW Building）的业务、项目、客户或经营情况。
- 地名、公司名、标准编号保留英文原文（如 Padstow、DW Building、AS 1288），不要音译。"""

AVOID_TMPL = """【去重要求】下面是最近几期已经讲过的内容。不要重复；如果某件事有重大新进展，只讲新进展，并一句话带过背景：
{items}"""


def load_history():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {"episodes": []}


def next_topic(pool, used, key):
    for t in pool:
        if t not in used:
            return t
    return None  # 主题库用完，交给模型自选


def section_prompt(ctx, title, minutes, brief, avoid):
    n = int(minutes * CHARS_PER_MIN)
    return f"""你是一档中文通勤音频节目的撰稿人。今天是北京时间 {ctx['date']}，这是{ctx['slot_cn']}（{ctx['slot_focus']}）。

{ctx['profile']}

{STYLE}

【本段任务：{title}】
{brief}

{avoid}

只依据上面提供的新闻素材、行情数据和你有把握的背景知识来写，素材里没有的最新事实不要编造，挑最重要的讲而不是逐条罗列。直接输出这一段的口播稿，{int(n*0.9)} 到 {int(n*1.1)} 字（严格控制篇幅，这一段约 {minutes} 分钟），不要加标题，不要开场白和过渡语，第一句直接进入内容。
口播稿写完后，单独一行写 ===要点===，下一行给出一个 JSON 数组，每个元素形如 {{"title": "15 字以内的中文短标题", "src": 所用素材的编号}}，列出本段讲到的每条新闻或主题，没有用素材时 src 写 0，用于以后查重。"""


def parse(text):
    if "===要点===" in text:
        body, tail = text.split("===要点===", 1)
        m = re.search(r"\[.*\]", tail, re.S)
        try:
            heads = json.loads(m.group(0)) if m else []
        except ValueError:
            heads = []
    else:
        body, heads = text, []
    body = re.sub(r"[*#>`_]+", "", body).strip()
    return body, [h if isinstance(h, dict) else {"title": str(h), "src": 0} for h in heads]


def write_section(ctx, title, minutes, brief, avoid, items=()):
    text, _ = gemini.generate(section_prompt(ctx, title, minutes, brief, avoid))
    body, raw = parse(text)
    heads, used, sources = [], [], []
    for h in raw:
        t, src = str(h.get("title", "")).strip(), h.get("src", 0)
        item = items[src - 1] if isinstance(src, int) and 0 < src <= len(items) else None
        if t:
            heads.append(t)
            used.append({"title": t, "source": item["title"] if item else ""})
        if item:
            sources.append({"title": item["title"], "uri": item["link"]})
    target = minutes * CHARS_PER_MIN
    try:
        body = adjust_length(body, target)
    except RuntimeError as e:  # 扩写/压缩只是锦上添花，失败就用原稿
        print(f"  {title}: 调整篇幅失败，用原稿 {e}")
    print(f"  {title}: {len(body)} 字, {len(heads)} 条要点")
    return {"title": title, "body": body, "heads": heads, "used": used, "sources": sources,
            "plan": (title, minutes, brief, avoid, items)}


def adjust_length(body, target):
    if len(body) < target * 0.7:  # 太短就让模型扩写一次
        more, s2 = gemini.generate(
            f"下面这段口播稿只有 {len(body)} 字，请在不改变事实的前提下扩写到约 {int(target)} 字，"
            f"补充具体事实、数据和对听众的实际影响，不要加废话。保持简洁播报风格，直接输出扩写后的全文，不要任何说明。\n\n{body}",
            search=False)
        body = re.sub(r"[*#>`_]+", "", more).strip()
    if len(body) > target * 1.1:  # 太长就压缩，保证整期约 20 分钟
        short, _ = gemini.generate(
            f"下面这段口播稿有 {len(body)} 字，请压缩到约 {int(target)} 字：保留最重要的事实、判断和对听众的影响，"
            f"删掉次要细节和铺垫。保持简洁播报风格，直接输出压缩后的全文，不要任何说明。\n\n{body}", search=False)
        body = re.sub(r"[*#>`_]+", "", short).strip()
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["morning", "evening"], required=True)
    ap.add_argument("--date", default=dt.datetime.now(BJT).strftime("%Y-%m-%d"))
    args = ap.parse_args()

    hist = load_history()
    eps = hist["episodes"]
    if any(e["date"] == args.date and e["slot"] == args.slot for e in eps):
        print("本期已生成过，跳过")
        return

    profile = os.environ.get("PROFILE_MD", "").strip()
    finance = os.environ.get("FINANCE_MD", "").strip()
    ctx = {
        "date": args.date,
        "slot_cn": SLOT_CN[args.slot],
        "slot_focus": "重点：隔夜美欧市场与全球大事、中国早间政策与经济数据" if args.slot == "morning"
        else "重点：澳洲当天新闻、中国白天动态、欧洲开盘前瞻",
        "profile": "【听众档案】\n" + (profile or "准备移居悉尼，公司 DW Building 位于悉尼 Padstow，行业是幕墙、扶手栏杆、门窗；计划先在公司附近租房。"),
    }

    covered = dedupe.load(args.date)
    avoid = AVOID_TMPL.format(items="；".join(dedupe.titles(covered)) or "（暂无）")
    used = {k: [e.get(k) for e in eps if e.get(k)] for k in ("culture", "industry", "life")}

    picks = {}
    for key, pool in (("culture", topics.CULTURE), ("industry", topics.INDUSTRY), ("life", topics.LIFE)):
        picks[key] = next_topic(pool, used[key], key)

    def topic_brief(key, desc):
        if picks[key]:
            return f"本期主题：「{picks[key]}」。{desc}"
        return f"自选一个之前没讲过的主题（已讲过：{'；'.join(used[key])}）。{desc}"

    print("抓取新闻素材 ...")
    mat = {c: dedupe.fresh(news.gather(c, limit=60), covered)[:25] for c in news.FEEDS}
    market = news.market_snapshot()
    for c, items in mat.items():
        print(f"  {c}: {len(items)} 条")
    m = lambda c: "\n【今日新闻素材】\n" + news.as_text(mat[c])

    plan = [
        ("澳洲要闻", 2.5, "挑 3 条过去 24 小时内澳洲（优先悉尼/新州）最重要的新闻：利率、房租房价、移民签证、生活成本、政策等。新闻必须用“第一条，……第二条，……第三条，……”的方式依次编号播报，每条开头先用一句话说标题。" + m("australia"), avoid, mat["australia"]),
        ("澳洲文化与历史", 2.5, topic_brief("culture", "讲清事实和对融入当地的实际用处。"), avoid),
        ("行业观察", 3, topic_brief("industry", "面向在悉尼做幕墙、门窗、扶手栏杆的从业者：讲清规范/标准要点、相关大公司与竞争情况、近期景气度或大项目，尽量给具体可用的信息；如有本周相关行业新闻可先简短带一句。") + m("industry"), avoid, mat["industry"]),
        ("澳洲生活小课堂", 2.5, topic_brief("life", "讲实操：步骤、费用大概多少、常见坑、需要准备什么，帮助尽快适应悉尼生活。"), avoid),
        ("中国要闻", 2.5, "挑 3 条过去 24 小时中国最重要的新闻：经济数据、重大政策、中澳关系、人民币汇率等。新闻必须用“第一条，……第二条，……第三条，……”的方式依次编号播报，每条开头先用一句话说标题。" + m("china"), avoid, mat["china"]),
        ("全球大事", 2.5, "挑 3 条过去 24 小时全球最重要的事：地缘政治、美联储与美国经济、大宗商品等。新闻必须用“第一条，……第二条，……第三条，……”的方式依次编号播报，每条开头先用一句话说标题。" + m("world"), avoid, mat["world"]),
        ("理财观察", 2.5, ("固定跟踪三个品种：纳斯达克指数、标普500指数、黄金。根据下面的行情数据讲清最新点位和近一周、近一个月的涨跌，"
                         "再结合本期讲到的宏观形势（美联储与利率预期、美债收益率、美元、财报、地缘风险）分析各自的趋势和关键价位，"
                         "最后对三个品种分别给出今天的观察倾向：偏买入、偏卖出或观望，说明理由，以及什么情况下这个判断会失效。"
                         f"\n【听众理财策略】\n{finance or '关注纳斯达克、标普500和黄金三个品种的短线买卖时机。'}"
                       "\n结尾必须原话说一句：“以上只是我的个人观察，仅供参考，不构成投资建议，我也不是持牌理财顾问。”"
                         "\n【最新行情数据】\n" + market + m("markets")), "", mat["markets"]),
    ]

    print(f"生成 {args.date} {SLOT_CN[args.slot]} ...")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=int(os.environ.get("GEN_WORKERS", "2"))) as pool:
        def safe(row):
            try:
                return write_section(ctx, *row)
            except RuntimeError as e:
                print(f"  {row[0]}: 生成失败，本期跳过该段 {e}")
                return None
        sections = [x for x in pool.map(safe, plan) if x]
    if len(sections) < 4:
        raise RuntimeError(f"只写成 {len(sections)} 段，放弃本期")

    # 写完再查一次：让模型判断本期是否与 30 天内内容重复，重复的段落重写一次（理财每天要讲，不查）
    try:
        listing = "\n".join(f"{i}. {s['title']}：{'；'.join(s['heads'])}" for i, s in enumerate(sections) if s["title"] != "理财观察")
        verdict, _ = gemini.generate(f"""已播内容（最近 30 天）：\n{'；'.join(dedupe.titles(covered))}\n\n本期各段：\n{listing}\n\n判断本期哪些条目与已播内容讲的是同一件事或同一知识点（有重大新进展的不算）。只输出 JSON：{{{{"repeats": [{{{{"section": 段落序号, "title": "重复的条目"}}}}]}}}}，没有重复时 repeats 为空数组。""".replace("{{", "{").replace("}}", "}"))
        m = re.search(r"\{.*\}", verdict, re.S)
        repeats = json.loads(m.group(0)).get("repeats", []) if m else []
        by_sec = {}
        for r in repeats:
            by_sec.setdefault(int(r["section"]), []).append(str(r["title"]))
        for i, dup in by_sec.items():
            print(f"  查重：{sections[i]['title']} 重复 {dup}，重写")
            title, minutes, brief, av, items = sections[i]["plan"]
            sections[i] = write_section(ctx, title, minutes, brief + "\n\n【必须换掉，不得再讲】" + "；".join(dup), av or avoid, items)
    except (ValueError, KeyError, TypeError, IndexError, RuntimeError) as e:
        print("查重跳过：", repr(e))

    outline = "\n".join(f"{s['title']}：{'、'.join(s['heads'][:4])}" for s in sections)
    oc = ""
    try:
        oc, _ = gemini.generate(f"""你是「澳洲新生活·每日观察」的主播。今天北京时间 {args.date}，{SLOT_CN[args.slot]}。本期各段内容要点：
{outline}

{STYLE}

只输出一个 JSON 对象，不要任何其他文字：
{{"opening": "开场，约 100 字：一句报日期和早报/晚报，然后直接列出今天三个最重要的看点，不寒暄",
  "closing": "结尾，约 120 字：三条今天的核心结论，加一件今天可以做的事，一句结束语",
  "theme": "本期最重要的一条新闻的主题，4 到 10 个汉字，用于文件名，不要标点"}}""", search=False)
    except RuntimeError as e:
        print("开场/结尾生成失败，用默认文案：", e)
    m = re.search(r"\{.*\}", oc, re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
    except ValueError:
        j = {}
    clean = lambda t: re.sub(r"[*#>`_]+", "", str(t or "")).strip()
    opening = clean(j.get("opening")) or f"今天是{args.date}，{SLOT_CN[args.slot]}。"
    closing = clean(j.get("closing")) or "以上是今天的内容，下期见。"
    theme = re.sub(r"[^\w一-鿿]", "", clean(j.get("theme")))[:10] or re.sub(r"[^\w一-鿿]", "", (sections[0]["heads"] or [sections[0]["title"]])[0])[:10]

    name = f"{args.date}_{SLOT_CN[args.slot]}_{theme}"
    spoken = "\n\n".join([opening] + [s["body"] for s in sections] + [closing])

    md = [f"# {name}\n", f"> 澳洲新生活·每日观察 · {args.date} {SLOT_CN[args.slot]} · 约 {len(spoken)//CHARS_PER_MIN} 分钟\n",
          "## 开场\n", opening, ""]
    for s in sections:
        md += [f"## {s['title']}\n", s["body"], ""]
    md += ["## 结尾\n", closing, "", "## 参考来源\n"]
    seen = set()
    for s in sections:
        for src in s["sources"]:
            if src.get("uri") not in seen:
                seen.add(src.get("uri"))
                md.append(f"- [{src.get('title', '来源')}]({src.get('uri')})")

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name + ".md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    with open(os.path.join(OUT, name + ".txt"), "w", encoding="utf-8") as f:
        f.write(spoken)

    meta = {"name": name, "date": args.date, "slot": args.slot, "slot_cn": SLOT_CN[args.slot], "theme": theme,
            "chars": len(spoken), "outline": [{"title": s["title"], "heads": s["heads"]} for s in sections]}
    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    eps.append({"date": args.date, "slot": args.slot, "name": name,
                "heads": [h for s in sections if s["title"] != "理财观察" for h in s["heads"]],
                **{k: picks[k] or next(((s["heads"] or [""])[0] for s in sections if s["title"] == t), "")
                   for k, t in (("culture", "澳洲文化与历史"), ("industry", "行业观察"), ("life", "澳洲生活小课堂"))
                   if picks[k] or any(s["title"] == t for s in sections)}})
    hist["episodes"] = eps[-200:]
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    dedupe.append([{"date": args.date, "slot": args.slot, "section": s["title"], "title": u["title"], "source": u["source"]}
                   for s in sections if s["title"] != "理财观察" for u in s["used"]])
    print(f"完成：{name}（{len(spoken)} 字）")


if __name__ == "__main__":
    main()
