"""Gemini REST 调用（带 Google 搜索落地），只用标准库。"""
import json
import os
import re
import time
import urllib.error
import urllib.request

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_candidates = None
_dead = set()


def candidates():
    """可用模型按优先级排序：GEMINI_MODEL 优先，其后新版 flash → flash-lite → 其他。
    免费档里新模型常常没有额度，遇到额度超限会依次换下一个。"""
    global _candidates
    if _candidates is not None:
        return _candidates
    req = urllib.request.Request(BASE + "?pageSize=1000", headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        models = [m["name"].split("/", 1)[1] for m in json.load(r).get("models", [])
                  if "generateContent" in m.get("supportedGenerationMethods", [])]
    models = [m for m in models if m.startswith("gemini-")
              and not any(k in m for k in ("image", "tts", "audio", "live", "embedding", "robotics", "computer"))]

    def score(n):
        v = re.match(r"gemini-(\d+(?:\.\d+)?)", n)
        tier = 2 if "flash" in n and "lite" not in n else 1 if "lite" in n else 0
        stable = "preview" not in n and "exp" not in n
        return (tier, stable, float(v.group(1)) if v else 0)

    ordered = sorted(models, key=score, reverse=True)
    want = os.environ.get("GEMINI_MODEL", "").strip()
    if want in ordered:
        ordered.remove(want)
        ordered.insert(0, want)
    _candidates = ordered
    print("候选模型:", ", ".join(ordered[:8]))
    return _candidates


def _call(model, body):
    req = urllib.request.Request(
        "%s/%s:generateContent" % (BASE, model),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    print("  [%s] %.0fs" % (model, time.time() - t0), flush=True)
    return data


def generate(prompt, search=False, temperature=0.7, max_tokens=8192, rounds=4):
    """返回 (正文, 来源列表[{title, uri}])。各模型轮流试：高峰期 503 很常见，不在一个模型上死等。"""
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if search:
        body["tools"] = [{"google_search": {}}]
    for attempt in range(rounds):
        for model in list(candidates())[:6]:
            if model in _dead:
                continue
            try:
                data = _call(model, body)
                cand = data["candidates"][0]
                text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
                if not text.strip():
                    raise ValueError("empty response: %s" % cand.get("finishReason"))
                chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
                return text.strip(), [c["web"] for c in chunks if "web" in c]
            except urllib.error.HTTPError as e:
                msg = e.read().decode(errors="ignore")
                if e.code in (400, 403, 404) or (e.code == 429 and ("limit: 0" in msg or "PerDay" in msg)):
                    _dead.add(model)  # 额度为 0 或日额度用完，本次运行不再试
                print("%s %s，换下一个" % (model, e.code), flush=True)
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as e:
                print("%s 出错 %r，换下一个" % (model, e), flush=True)
            time.sleep(3)
        print("第 %d 轮全部失败，等待 60s" % (attempt + 1), flush=True)
        time.sleep(60)
    raise RuntimeError("所有 Gemini 模型都不可用")
