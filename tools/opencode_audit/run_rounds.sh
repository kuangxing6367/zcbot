#!/usr/bin/env bash
# zcbot 主线 16 轮 opencode 代码缺陷审计（串行：上一轮完成才开下一轮）
# 用法：bash tools/opencode_audit/run_rounds.sh
set -u

cd "$(dirname "$0")/../.." || exit 1
DIR="tools/opencode_audit"
OC="/c/Users/wb/AppData/Roaming/ai.opencode.desktop/cli/2.3.0-x64/opencode-cli.exe"
LOG="$DIR/run.log"
MODEL="opencode/big-pickle"
MAX_TRY=5

echo "=== 审计开始 $(date) ===" >> "$LOG"

for i in $(seq -w 1 16); do
  REPORT="$DIR/audit_round_${i}.md"
  if [ -s "$REPORT" ]; then
    echo "[$(date +%H:%M:%S)] 第 $i 轮已有报告，跳过" >> "$LOG"
    continue
  fi

  ok=0
  for attempt in $(seq 1 "$MAX_TRY"); do
    echo "[$(date +%H:%M:%S)] 第 $i 轮 attempt $attempt" >> "$LOG"
    timeout 300 "$OC" run --auto --model "$MODEL" \
      "只读 tools/opencode_audit/BRIEF.md、tools/opencode_audit/round_${i}.md 和它指定的摘录文件，不要读其它文件。严格按 BRIEF.md 执行，把报告写到 tools/opencode_audit/audit_round_${i}.md。不要改产品代码、不要打 tag、不要发版。" \
      >> "$LOG" 2>&1
    if [ -s "$REPORT" ]; then
      echo "[$(date +%H:%M:%S)] 第 $i 轮完成 ($(wc -c < "$REPORT") 字节)" >> "$LOG"
      ok=1
      break
    fi
    echo "[$(date +%H:%M:%S)] 第 $i 轮 attempt $attempt 未产出，重试" >> "$LOG"
  done

  if [ "$ok" -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] 第 $i 轮失败（$MAX_TRY 次均未产出报告）" >> "$LOG"
  fi
done

echo "=== 审计结束 $(date) ===" >> "$LOG"
echo "报告数: $(ls "$DIR"/audit_round_*.md 2>/dev/null | wc -l)/16"
