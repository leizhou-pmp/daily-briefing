#!/usr/bin/env bash
# 云端上传到夸克网盘。登录态加密存放在 state/quark.enc（密钥为 QUARK_KEY），
# 每次运行后若 token 被刷新则重新加密写回，由工作流提交。
# 用法: bash briefing/quark_upload.sh out
set -uo pipefail
OUT="$1"
SK="$HOME/quark-skill"
NAME=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['name'])" "$OUT/meta.json")

# CLI 由工作流缓存；从海外连夸克偶尔超时，多试几次
if [ ! -f "$SK/scripts/quark-drive.cjs" ]; then
  resp=$(curl -fsSL --retry 4 --retry-all-errors --connect-timeout 60 --max-time 120 "https://open-api-drive.quark.cn/agent/v1/skill_config?req_id=$(date +%s)$RANDOM")
  url=$(node -e 'const j=JSON.parse(process.argv[1]);const c=(j.data&&j.data.config)||j.config||j.data||j;process.stdout.write(String(c.qkPan||"").trim())' "$resp")
  tmp=$(mktemp -d)
  curl -fsSL --retry 4 --retry-all-errors --connect-timeout 60 --max-time 300 -o "$tmp/q.zip" "$url"
  unzip -qo "$tmp/q.zip" -d "$tmp/x"
  cli=$(find "$tmp/x" -name quark-drive.cjs | head -1)
  mkdir -p "$SK"
  cp -R "$(dirname "$(dirname "$cli")")"/. "$SK"/
fi

# 用独立的 codex 身份运行，与本机 Claude Code 的登录互不影响
mkdir -p "$SK/codex"
openssl enc -d -aes-256-cbc -pbkdf2 -in state/quark.enc -out "$SK/codex/config.json" -pass env:QUARK_KEY
before=$(shasum "$SK/codex/config.json" | cut -d' ' -f1)

# 只传 mp3；连同之前失败的期一起传（state/quark_pending.txt），偶发 1007 时重试
PENDING=state/quark_pending.txt
touch "$PENDING"
AUDIO="site/$PAGES_TOKEN/audio"
: > "$OUT/still_pending.txt"
for n in $(cat "$PENDING") "$NAME"; do
  ok=
  [ -f "$AUDIO/$n.mp3" ] || { echo "找不到 $n.mp3，跳过"; continue; }
  for i in 1 2 3 4; do
    env -u CLAUDECODE -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_ENTRY_POINT -u AI_AGENT CODEX_CI=1 \
      node "$SK/scripts/quark-drive.cjs" upload "$AUDIO/$n.mp3" --parent-fid "$QUARK_FOLDER_FID" \
      > "$OUT/quark.log" 2>&1 || true
    grep '"type":"result"' "$OUT/quark.log" | tail -1 | cut -c1-200
    if grep '"type":"result"' "$OUT/quark.log" | tail -1 | grep -q '"code":0'; then ok=1; break; fi
    echo "第 $i 次上传失败，60 秒后重试"; sleep 60
  done
  [ -n "$ok" ] && echo "已上传 $n" || echo "$n" >> "$OUT/still_pending.txt"
done
sort -u "$OUT/still_pending.txt" > "$PENDING"

after=$(shasum "$SK/codex/config.json" | cut -d' ' -f1)
if [ "$before" != "$after" ]; then
  openssl enc -aes-256-cbc -pbkdf2 -salt -in "$SK/codex/config.json" -out state/quark.enc -pass env:QUARK_KEY
  echo "夸克登录态已刷新并重新加密"
fi
rm -f "$SK/codex/config.json"

[ ! -s "$PENDING" ] || { echo "夸克上传失败，下期补传：$(cat "$PENDING")"; exit 1; }
echo "已上传夸克网盘"
