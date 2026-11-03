"""发邮件：正文只放倍速播放页链接和提纲，不带附件。

用法: python -m briefing.send_email out
"""
import html
import json
import os
import smtplib
import sys
import urllib.parse
from email.message import EmailMessage

MAIL_FROM = os.environ.get("MAIL_FROM", "siriuslei0812@gmail.com")
MAIL_TO = os.environ.get("MAIL_TO", "sirius0812@outlook.com")


def main():
    out = sys.argv[1]
    meta = json.load(open(os.path.join(out, "meta.json"), encoding="utf-8"))
    name = meta["name"]
    play_url = "%s/%s/?ep=%s" % (os.environ["PAGES_BASE"].rstrip("/"), os.environ["PAGES_TOKEN"], urllib.parse.quote(name))

    outline = "".join(
        "<li><b>%s</b>%s</li>" % (html.escape(s["title"]), "：" + html.escape("、".join(s["heads"][:4])) if s["heads"] else "")
        for s in meta["outline"])
    body = f"""<div style="font-family:-apple-system,'PingFang SC',sans-serif;font-size:16px;line-height:1.6">
<h2 style="margin:0 0 4px">{html.escape(meta['theme'])}</h2>
<div style="color:#666">澳洲新生活·每日观察 · {meta['date']} {meta['slot_cn']}</div>
<p><a href="{play_url}" style="display:inline-block;background:#0a7c66;color:#fff;padding:12px 20px;border-radius:10px;text-decoration:none;font-weight:600">▶ 打开倍速播放（0.8x–2x，记住进度）</a></p>
<ol>{outline}</ol>
<p style="color:#888;font-size:13px">音频同时存入夸克网盘「每日新闻播报」。</p>
</div>"""

    msg = EmailMessage()
    msg["Subject"] = f"【每日观察·{meta['slot_cn']}】{meta['date']} {meta['theme']}"
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg.set_content(f"{meta['theme']}\n倍速播放：{play_url}")
    msg.add_alternative(body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(MAIL_FROM, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)
    print("邮件已发送到", MAIL_TO)


if __name__ == "__main__":
    main()
