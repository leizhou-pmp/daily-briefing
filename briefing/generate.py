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

STYLE = """【节目定位】一档中文新闻节目，覆盖澳洲（悉尼/新州优先）、中国、国际大事，以及悉尼门窗、幕墙、扶手栏杆行业动态。

【写作风格：标准新闻播报体，言简意赅】
- 按电台/通讯社新闻写法：第三人称、客观陈述。每条新闻先用一句导语交代最核心的事实（谁、何时、何地、做了什么、关键数字），再用一到两句补充原因或背景，然后结束，进入下一条。
- 不使用第二人称，稿件中不得出现“你”“您”“听众”“大家”“我们”。听众档案只用于判断选题，不要在稿件里提及听众身份、处境或计划。
- 不分析“对个人的影响”，不给建议，不讲“可以怎么做”（生活小课堂和理财观察两段除外，且也要用客观陈述句）。
- 禁止使用以下词句及类似说法：对你而言、对你来说、对你的影响、对于……来说、这意味着、意味着什么、值得注意的是、值得关注的是、需要注意的是、说白了、简单来说、换句话说、总的来说、总而言之、不难看出、可以说、事实上、众所周知、我们来看、好的、接下来、下面、首先让我们、让我们。
- 不寒暄、不铺垫、不设问、不感慨、不做主观评价；删掉形容词、重复和空话，多给具体数字、名称、标准编号、价格、日期。
- 不要念网址、不要用列表符号、不要用 Markdown；数字口语化（“百分之四点一”），英文专有名词首次出现给中文。
- 只讲有依据的事实；不确定的不说。
- 绝对不要编造听众公司（DW Building）的业务、项目、客户或经营情况。
- 地名、公司名、标准编号保留英文原文（如 Padstow、DW Building、AS 1288），不要音译。"""

BANNED = ["对你而言", "对你来说", "对你的影响", "对您", "这意味着", "值得注意的是", "值得关注的是", "需要注意的是",
          "说白了", "简单来说", "换句话说", "总的来说", "总而言之", "不难看出", "众所周知", "我们来看", "让我们"]

AVOID_TMPL = """【去重要求：本月不重复】
一、话题层面。下面是最近 30 天讲过的话题及次数。标“禁止”的话题本期不要再讲；标“本月讲过”的话题，只有出现官方决定（如央行议息结果、法案通过）、官方数据发布或重大突发事件才可再讲，市场预期、评论、分析、预测、银行跟进调整等都不算新事件。即使可以再讲，也只讲新事实，背景最多一句。
{topics}

二、具体条目层面。下面是最近 30 天已经讲过的具体内容，不要重复：
{items}"""

TOPIC_CHOICES = "、".join(list(dedupe.TOPICS) + ["澳洲政治", "治安与法庭", "交通与基建", "教育", "医疗", "科技与AI",
                                               "能源与气候", "天气与灾害", "建筑行业", "中国经济", "中国政策",
                                               "地缘冲突", "国际政治", "企业与市场"])


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

只依据上面提供的新闻素材、行情数据和你有把握的背景知识来写，素材里没有的最新事实不要编造，挑最重要的讲而不是逐条罗列。直接输出这一段的口播稿，{int(n*0.9)} 到 {int(n*1.1)} 字（这一段约 {minutes} 分钟；素材不够时宁可多讲一条不同话题的新闻，也不要用空话凑字数），不要加标题，不要开场白和过渡语，第一句直接进入内容。
口播稿写完后，单独一行写 ===要点===，下一行给出一个 JSON 数组，每个元素形如 {{"title": "15 字以内的中文短标题", "topic": "话题", "src": 所用素材的编号}}，列出本段讲到的每条新闻或主题，没有用素材时 src 写 0。topic 从这些话题里选一个：{TOPIC_CHOICES}；都不合适时写“其他”。用于以后查重。"""


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
        topic = str(h.get("topic", "")).strip() or dedupe.classify(t)
        item = items[src - 1] if isinstance(src, int) and 0 < src <= len(items) else None
        if t:
            heads.append(t)
            used.append({"title": t, "source": item["title"] if item else "", "topic": topic})
        if item:
            sources.append({"title": item["title"], "uri": item["link"]})
    target = minutes * CHARS_PER_MIN
    try:
        body = adjust_length(body, target)
    except RuntimeError as e:  # 扩写/压缩只是锦上添花，失败就用原稿
        print(f"  {title}: 调整篇幅失败，用原稿 {e}")
    body = scrub(body)
    print(f"  {title}: {len(body)} 字, {len(heads)} 条要点")
    return {"title": title, "body": body, "heads": heads, "used": used, "sources": sources,
            "plan": (title, minutes, brief, avoid, items)}


def scrub(body):
    """去掉“对你而言”这类口头禅和第二人称：有就让模型改写一次，再用正则兜底删连接词。"""
    hits = [w for w in BANNED if w in body] + (["你"] if re.search(r"(?<!迷)你", body) else [])
    if hits:
        try:
            fixed, _ = gemini.generate(
                f"把下面这段新闻口播稿改成标准新闻播报体：删除“{'”“'.join(hits)}”等说法，不使用第二人称，"
                f"删除针对个人的影响分析和建议，事实、数字、顺序不变，篇幅基本不变。直接输出改好的全文，不要任何说明。\n\n{body}",
                search=False, temperature=0.3)
            body = re.sub(r"[*#>`_]+", "", fixed).strip() or body
        except RuntimeError as e:
            print("  去口头禅失败，用原稿", e)
    body = re.sub(r"(对你|对您)(而言|来说)[，,]?", "", body)
    body = re.sub(r"(值得注意|值得关注|需要注意)的是[，,]?", "", body)
    body = re.sub(r"(说白了|简单来说|换句话说|总的来说|总而言之|不难看出|众所周知)[，,]?", "", body)
    return body


def adjust_length(body, target):
    if len(body) < target * 0.6:  # 明显太短才扩写；新闻体宁短勿水
        more, s2 = gemini.generate(
            f"下面这段新闻口播稿只有 {len(body)} 字，请在不改变事实的前提下扩写到约 {int(target)} 字，"
            f"只能补充相关的背景事实、数据、时间线或当事方表态，不要加评论、建议和对个人影响的分析，不要加空话。"
            f"保持标准新闻播报体，直接输出扩写后的全文，不要任何说明。\n\n{body}",
            search=False)
        body = re.sub(r"[*#>`_]+", "", more).strip()
    if len(body) > target * 1.1:  # 太长就压缩，保证整期约 20 分钟
        short, _ = gemini.generate(
            f"下面这段新闻口播稿有 {len(body)} 字，请压缩到约 {int(target)} 字：保留最重要的事实和数字，"
            f"删掉次要细节、铺垫和评论。保持标准新闻播报体，直接输出压缩后的全文，不要任何说明。\n\n{body}", search=False)
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
    avoid = AVOID_TMPL.format(topics=dedupe.topic_text(covered, args.date),
                              items="；".join(dedupe.titles(covered)) or "（暂无）")
    used = {k: [e.get(k) for e in eps if e.get(k)] for k in ("culture", "industry", "life")}

    picks = {}
    for key, pool in (("culture", topics.CULTURE), ("industry", topics.INDUSTRY), ("life", topics.LIFE)):
        picks[key] = next_topic(pool, used[key], key)

    def topic_brief(key, desc):
        if picks[key]:
            return f"本期主题：「{picks[key]}」。{desc}"
        return f"自选一个之前没讲过的主题（已讲过：{'；'.join(used[key])}）。{desc}"

    print("抓取新闻素材 ...")
    mat = {c: ([it for it in news.gather(c, limit=60) if not dedupe.is_covered(it["title"], covered)] if c == "markets"
               else dedupe.fresh(news.gather(c, limit=60), covered, args.date))[:25]
           for c in news.FEEDS}
    market = news.market_snapshot()
    for c, items in mat.items():
        print(f"  {c}: {len(items)} 条")
    m = lambda c: "\n【今日新闻素材】\n" + news.as_text(mat[c])
    numbered = "三条新闻必须分属三个不同话题。用“第一条，……第二条，……第三条，……”依次播报，每条第一句就是导语。"

    plan = [
        ("澳洲要闻", 2.5, "挑 3 条过去 24 小时内澳洲（优先悉尼/新州）新闻价值最高的新闻，话题不限，按新闻价值选，不要固定讲某几类。" + numbered + m("australia"), avoid, mat["australia"]),
        ("澳洲文化与历史", 2.5, topic_brief("culture", "讲清史实、现状和关键数字，客观陈述。"), avoid),
        ("行业观察", 3, topic_brief("industry", "内容范围：悉尼幕墙、门窗、扶手栏杆相关的规范/标准要点、主要公司与竞争格局、行业景气度或大项目，给具体名称、编号、数字；如有本周相关行业新闻可先用一两句报道。") + m("industry"), avoid, mat["industry"]),
        ("澳洲生活小课堂", 2.5, topic_brief("life", "讲实操信息：办理步骤、费用、所需材料、常见问题，用客观陈述句，不用“你”。"), avoid),
        ("中国要闻", 2.5, "挑 3 条过去 24 小时中国新闻价值最高的新闻，话题不限，只讲中国本土的事（澳洲本地新闻不放这里）。" + numbered + m("china"), avoid, mat["china"]),
        ("全球大事", 2.5, "挑 3 条过去 24 小时全球新闻价值最高的事，话题不限，不讲澳洲和中国本土新闻。" + numbered + m("world"), avoid, mat["world"]),
        ("理财观察", 2.5, ("固定跟踪三个品种：纳斯达克指数、标普500指数、黄金。根据下面的行情数据报出最新点位和近一周、近一个月的涨跌，"
                         "再结合宏观因素（美联储与利率预期、美债收益率、美元、财报、地缘风险）简要分析各自趋势和关键价位，"
                         "宏观背景只讲当天的新变化，不要每天重复同样的分析。"
                         "最后对三个品种分别给出今天的观察倾向：偏买入、偏卖出或观望，一句理由，以及判断失效的条件。"
                         f"\n【理财策略】\n{finance or '关注纳斯达克、标普500和黄金三个品种的短线买卖时机。'}"
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

    # 写完再查一次：让模型判断本期是否与 30 天内内容重复（同一件事或同一话题），重复的段落重写一次（理财每天要讲，不查）
    try:
        listing = "\n".join(f"{i}. {s['title']}：" + "；".join(f"{u['title']}（话题：{u['topic']}）" for u in s["used"])
                            for i, s in enumerate(sections) if s["title"] != "理财观察")
        verdict, _ = gemini.generate(f"""已播话题统计（最近 30 天）：\n{dedupe.topic_text(covered, args.date)}\n\n已播条目（最近 30 天）：\n{'；'.join(dedupe.titles(covered))}\n\n本期各段：\n{listing}\n\n逐条判断本期哪些条目算重复，以下情况都算重复：1）与已播条目讲的是同一件事或同一知识点；2）所属话题 30 天内讲过，且本条不是官方决定、官方数据发布或重大突发（市场预期、评论、分析、预测、银行跟进调整都不算新事件）；3）本期不同段落之间讲了同一话题，保留第一处，后面的算重复。只输出 JSON：{{{{"repeats": [{{{{"section": 段落序号, "title": "重复的条目"}}}}]}}}}，没有重复时 repeats 为空数组。""".replace("{{", "{").replace("}}", "}"))
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
{{"opening": "开场，约 80 字：一句报日期和早报/晚报，然后用新闻提要的方式列出今天三条最重要的消息，不寒暄",
  "closing": "结尾，约 60 字：一句话回顾本期最重要的一条消息，然后一句结束语。不做总结评论，不给建议",
  "theme": "本期最重要的一条新闻的主题，4 到 10 个汉字，用于文件名，不要标点"}}""", search=False)
    except RuntimeError as e:
        print("开场/结尾生成失败，用默认文案：", e)
    m = re.search(r"\{.*\}", oc, re.S)
    try:
        j = json.loads(m.group(0)) if m else {}
    except ValueError:
        j = {}
    clean = lambda t: re.sub(r"(对你|对您)(而言|来说)[，,]?", "", re.sub(r"[*#>`_]+", "", str(t or ""))).strip()
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
    dedupe.append([{"date": args.date, "slot": args.slot, "section": s["title"], "title": u["title"], "source": u["source"],
                    "topic": u.get("topic") or dedupe.classify(u["title"] + " " + u["source"])}
                   for s in sections if s["title"] != "理财观察" for u in s["used"]])
    print(f"完成：{name}（{len(spoken)} 字）")


if __name__ == "__main__":
    main()
