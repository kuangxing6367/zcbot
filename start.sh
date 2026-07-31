#!/bin/bash

# ============================================================
# ZCBOT OneBot 框架启动脚本
# 用法: bash start.sh
# ============================================================

# 项目目录（脚本所在目录）
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"

cd "$PROJECT_DIR"

# 检查 Python 版本
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 python3，请先安装 Python 3.8+"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python 版本: $PY_VERSION"

# 创建虚拟环境（首次运行）
if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
fi

# 激活虚拟环境
source "$VENV_DIR/bin/activate"

# 安装/更新依赖（走清华源，失败自动回退）
echo "检查依赖..."
pip install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple
pip install -r requirements.txt -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn

# 启动框架
echo "启动 ZCBOT..."
python3 main.py
