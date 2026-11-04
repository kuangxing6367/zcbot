#!/bin/bash
# ============================================================
# ZCBOT 远程部署脚本（宝塔 / Linux 服务器）
#
# 做法：本地打包「代码」→ 上传 → 服务器备份 → 解包 → 重启 → 验收。
# 设计要点（都是踩过的坑，改脚本前先读）：
#   1) 只同步**代码**：config.yaml / core_plugins.yaml / data/ / plugins/ 一律不动，
#      避免覆盖线上的 MySQL 配置、数据库与用户插件；
#   2) **排除原生库**（*.so/*.pyd/image_renderer native）：本地可能是别的平台构建，
#      覆盖会直接让线上渲染/加速模块崩掉；
#   3) 用 tar 解包（无 --delete 语义），线上独有的文件（如 start.sh）不会被删；
#   4) 重启时用**完整路径前缀**匹配进程。用宽泛的 `zcbot` 会把同机其它站点
#      （如 dbcj_zcbot）一起杀掉——务必用 REMOTE_DIR 全路径；
#   5) web.port 若被宝塔占用（8080 默认归 BT-Panel）需改成空闲端口。
#
# 用法：
#   SSH_KEY=/path/to/key SSH_HOST=root@1.2.3.4 \
#   REMOTE_DIR=/www/wwwroot/bot.zgric.top/zcbot WEB_PORT=6080 \
#   bash tools/deploy_remote.sh
#
# 只演练不上传：DRY_RUN=1 ...
# ============================================================
set -eu

SSH_KEY="${SSH_KEY:?请设置 SSH_KEY（私钥路径）}"
SSH_HOST="${SSH_HOST:?请设置 SSH_HOST（如 root@1.2.3.4）}"
REMOTE_DIR="${REMOTE_DIR:?请设置 REMOTE_DIR（线上代码目录）}"
WEB_PORT="${WEB_PORT:-6080}"
PY_BIN="${PY_BIN:-python3}"
DRY_RUN="${DRY_RUN:-0}"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SSH_OPT="-i $SSH_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes"
ARCHIVE="$PROJECT_DIR/tmp/zcbot_deploy.tar.gz"

say() { printf '\n=== %s ===\n' "$1"; }

say "打包代码（排除配置 / 数据 / 原生库）"
cd "$PROJECT_DIR"
mkdir -p tmp
tar czf "$ARCHIVE" \
  --exclude='./.git' --exclude='./.git/*' \
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
  --exclude='./config.yaml' --exclude='./core_plugins.yaml' \
  --exclude='./data' --exclude='./data/*' \
  --exclude='./plugins' --exclude='./plugins/*' \
  --exclude='./tmp' --exclude='./tmp/*' \
  --exclude='./db' --exclude='./db/*' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='./venv' --exclude='./.venv' \
  --exclude='./.workbuddy' --exclude='./.workbuddy-ai' \
  --exclude='./.kilo' --exclude='./.commandcode' \
  --exclude='node_modules' \
  --exclude='*.so' --exclude='*.pyd' --exclude='*.dll' --exclude='*.dylib' \
  --exclude='./core_plugins/image_renderer/native' \
  --exclude='./core_plugins/rust_accel/native' \
  .
ls -lh "$ARCHIVE" | awk '{print "  归档:", $5}'

# 安全校验：敏感文件绝不能进包
for f in ./config.yaml ./core_plugins.yaml; do
  if tar tzf "$ARCHIVE" | grep -qx -- "$f"; then
    echo "  !! 归档误含 $f，已中止"; rm -f "$ARCHIVE"; exit 1
  fi
done
echo "  已确认不含 config.yaml / core_plugins.yaml"

if [ "$DRY_RUN" = "1" ]; then
  say "DRY_RUN=1，跳过上传/重启"
  exit 0
fi

say "上传"
scp $SSH_OPT "$ARCHIVE" "$SSH_HOST:/root/" >/dev/null

say "服务器：备份 → 解包 → 校正端口 → 重启 → 验收"
ssh $SSH_OPT "$SSH_HOST" "REMOTE_DIR='$REMOTE_DIR' WEB_PORT='$WEB_PORT' PY_BIN='$PY_BIN' bash -s" <<'REMOTE'
set -eu
D="$REMOTE_DIR"; PY="$(command -v $PY_BIN)"
TS=$(date +%Y%m%d-%H%M%S); BK=/root/zcbot-backup-$TS
mkdir -p "$BK"
cp -a "$D/config.yaml" "$BK/" 2>/dev/null || true
cp -a "$D/core_plugins.yaml" "$BK/" 2>/dev/null || true
tar czf "$BK/code.tar.gz" -C "$D" --exclude='./data' --exclude='./.git' . 2>/dev/null || true
echo "$BK" > /root/.last_zcbot_backup
echo "  备份: $BK"

tar xzf /root/zcbot_deploy.tar.gz -C "$D"
echo "  解包完成"

# 校正 web 端口（宝塔常占 8080）
if grep -q '^web:' "$D/config.yaml"; then
  L=$(grep -n '^web:' "$D/config.yaml" | head -1 | cut -d: -f1)
  sed -i "$((L+1))s|.*|  host: 0.0.0.0|" "$D/config.yaml"
  sed -i "$((L+2))s|.*|  port: $WEB_PORT|" "$D/config.yaml"
  sed -n "$((L)),$((L+2))p" "$D/config.yaml"
fi

# 重启：只匹配 REMOTE_DIR 全路径，避免误杀同机其它站点
PAT="$D/"
for pid in $(pgrep -f "$PAT" || true); do kill "$pid" 2>/dev/null || true; done
sleep 4
pgrep -f "$PAT" >/dev/null 2>&1 && { pkill -9 -f "$PAT" || true; sleep 2; }

cd "$D"
find . -name '__pycache__' -type d -not -path './.git/*' -prune -exec rm -rf {} + 2>/dev/null || true
mkdir -p data/logs
setsid nohup "$PY" main.py >> data/logs/zcbot.out 2>&1 &
for i in $(seq 1 15); do
  sleep 2
  ss -lnt 2>/dev/null | grep -q ":$WEB_PORT" && { echo "  $WEB_PORT 已监听 ✓"; break; }
done

# 验收
echo -n "  VERSION: "; cat VERSION
printf "  HTTP: "; curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "http://127.0.0.1:$WEB_PORT/" || echo fail
echo -n "  Traceback 次数: "; grep -ac 'Traceback' data/logs/zcbot.out 2>/dev/null || echo 0
grep -aE '调试控制台|启动完成' data/logs/zcbot.out | tail -2
REMOTE

say "完成"
echo "  如未启用 aiwriter：在 core_plugins.yaml 把 aiwriter.enabled 设为 true 后重载"
echo "  接入调试控制台：ssh 上去执行  $PY_BIN main.py attach"
