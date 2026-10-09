#!/usr/bin/env bash
# 工作区护栏：污染守卫 + 测试残留守卫
# 由 .github/workflows/ci.yml 与 tests.yml 在测试步骤之后调用（共用，避免两份内联脚本漂移）。
#
# 设计约定：
#   1. 污染守卫基于 git status --porcelain：已被 .gitignore 覆盖、工作流正常
#      运行会产生的文件（__pycache__/、.pytest_cache/ 等）不会出现在其中，
#      天然放行——即「允许工作流自身正常产生已忽略文件」；
#   2. 项目有意跟踪的发布二进制（core_plugins/image_renderer/native/bin 下的
#      zcbot_render.so/.pyd，由 build-zcbot-render.yml 产物手工落位入库）显式
#      例外：若后续步骤就地刷新它们，不算污染；
#   3. 测试残留守卫直接扫描文件系统，因此能抓到「已被 .gitignore 忽略」的残留：
#      test_perm.py（tests/_perm_test_*.db 及 -wal/-shm 侧车）与
#      test_api_security.py（根 _sec_dl_tmp.db）自身带 finally 清理契约，
#      这些模式一旦出现即代表契约被打破；根 render_test*.png 与
#      *.bak/*.orig/*.rej/*.tmp.* 为手工/合并/编辑器残留，工作流不会正常
#      产生，出现即报错；
#   4. 扫描跳过 .git、node_modules 与发布二进制目录（native/bin 下的 .bak_*
#      二进制备份属本地回滚资产，不参与 CI 判断）。
#
# 注意：本脚本面向 CI 的干净检出场景；在本地脏工作区直接运行会把既有
# 未跟踪文件（如 .commandcode/、core_plugins/aiwriter/ 等）计为污染。
set -uo pipefail

RELEASE_BIN_DIR="core_plugins/image_renderer/native/bin"
fail=0

# ---------- 污染守卫：不得留下未忽略的新文件，或改动/删除跟踪文件 ----------
pollution=$(git status --porcelain=v1 | grep -v "${RELEASE_BIN_DIR}/" || true)
if [ -n "$pollution" ]; then
  echo "::error::污染守卫失败：工作流在工作区留下了未忽略的新文件，或改动/删除了跟踪文件"
  printf '%s\n' "$pollution"
  fail=1
fi

# ---------- 测试残留守卫：已知残留模式即使被忽略也不允许出现 ----------
residue=$(
  {
    find tests -maxdepth 1 -name '_perm_test_*.db*' -print 2>/dev/null
    find . -maxdepth 1 \( -name 'render_test*' -o -name '_sec_dl_tmp.db' \) -print 2>/dev/null
    find . \( -name .git -o -name node_modules -o -path "./${RELEASE_BIN_DIR}" \) -prune -o \
      \( -name '*.bak' -o -name '*.orig' -o -name '*.rej' -o -name '*.tmp.*' \) -print 2>/dev/null
  } | sort -u
)
if [ -n "$residue" ]; then
  echo "::error::测试残留守卫失败：发现测试/合并/编辑器残留文件"
  printf '%s\n' "$residue"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo "::error::工作区护栏未通过（明细见上方输出）"
fi
exit "$fail"
