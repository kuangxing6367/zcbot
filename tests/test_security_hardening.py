# -*- coding: utf-8 -*-
"""v1.7.3 安全加固回归测试。

覆盖 7 项安全修复：
  ① /api/db 敏感表（admin_users/api_tokens）对普通管理员隐藏与 403
  ② /api/files/download 禁止普通管理员下载 .db/.sqlite 等数据库文件
  ③ 插件上传/安装/更新、依赖安装、隔离环境、框架更新收紧为 super
  ④ 登录失败文案统一（禁用账号不再区分 401/403，防用户名枚举）
  ⑤ 500 错误回显收敛为「服务器内部错误」，不留 str(e)/{e}
  ⑥ ZIP 上传/更新条目校验（拒绝 ..、/ 开头、\\ 路径穿越，拒绝符号链接）
  ⑦ 文件浏览 _safe_file_path 解析符号链接防逃逸

运行：python tests/test_security_hardening.py  （可直接运行；也可被 pytest 收集）
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_DIR = os.path.join(REPO, 'framework', 'api')

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def read_api(name):
    with open(os.path.join(API_DIR, name), 'r', encoding='utf-8') as f:
        return f.read()


# ---- ① /api/db 敏感表 ----

def test_db_gateway_sensitive_tables():
    src = read_api('db_gateway.py')
    check("db_gateway 定义 _SENSITIVE_TABLES 且含 admin_users/api_tokens",
          "_SENSITIVE_TABLES = {'admin_users', 'api_tokens'}" in src)
    check("schema 接口对敏感表返回 403",
          "if table in _SENSITIVE_TABLES and not _is_super():" in src
          and "403" in src)
    check("rows 接口对敏感表返回 403",
          src.count("if table in _SENSITIVE_TABLES and not _is_super():") >= 2)
    check("tables 列表对普通管理员隐藏敏感表",
          "if not _is_super() and t in _SENSITIVE_TABLES:" in src)


# ---- ② files download 敏感扩展名 ----

def test_files_download_db_blocked_for_admin():
    src = read_api('files.py')
    check("download 定义敏感扩展名集合（.db/.sqlite/.sqlite3/wal/shm）",
          "sensitive_exts = {'.db', '.sqlite', '.sqlite3', '.db-wal', '.db-shm'}" in src)
    check("download 对非 super 命中敏感扩展名返回 403",
          "if ext in sensitive_exts and admin.get('role') != 'super':" in src)


# ---- ③ 高危操作收紧为 super ----

def test_sensitive_ops_require_super():
    plugins_src = read_api('plugins.py')
    market_src = read_api('plugin_market.py')
    fw_src = read_api('framework_update.py')

    def route_has_super(src, route, marker, decorator='@require_super'):
        idx = src.find(f"@app.route('{route}'")
        if idx < 0:
            return False, f"route {route} 未找到"
        seg = src[idx:idx + 600]
        return ('@require_super' in seg), f"route {route} 装饰器非 {decorator}（{marker}）"

    checks = [
        ('plugins.py', plugins_src, "/api/plugins/upload", "插件上传-代码执行入口"),
        ('plugins.py', plugins_src, "/api/plugins/<plugin_name>/install_deps", "pip 安装"),
        ('plugins.py', plugins_src, "/api/plugins/<plugin_name>/create_isolated_env", "venv 创建"),
        ('plugin_market.py', market_src, "/api/plugins/<plugin_name>/update", "GitHub 更新插件"),
        ('plugin_market.py', market_src, "/api/plugins/market/install", "市场安装插件"),
        ('framework_update.py', fw_src, "/api/framework/update", "框架更新"),
    ]
    for fname, src, route, marker in checks:
        ok, detail = route_has_super(src, route, marker)
        check(f"{fname} {route} 已收紧为 super（{marker}）", ok, detail)


# ---- ④ 登录文案统一 ----

def test_login_message_unified():
    src = read_api('auth.py')
    check("auth.py 禁用账号分支返回 401「用户名或密码错误」",
          "if not row['is_active']:" in src
          and "return jsonify({'code': 401, 'msg': '用户名或密码错误'}), 401" in src)
    check("禁用分支不再返回 403「账号已禁用」",
          "'账号已禁用'), 403" not in src)
    check("audit_log 仍保留真实禁用原因",
          "error_message='账号已禁用'" in src)


# ---- ⑤ 500 回显收敛 ----

def test_no_500_error_detail_leak():
    leaked = []
    for fname in os.listdir(API_DIR):
        if not fname.endswith('.py'):
            continue
        src = read_api(fname)
        # 命中模式：500 响应中直接拼接异常对象（str(e) / f'...{e}'）
        for m in re.finditer(r"jsonify\(\{['\"]code['\"]:\s*500,[^}]*['\"]msg['\"]:\s*(.*?)\},?\s*500\)",
                             src, re.S):
            seg = m.group(1)
            if ('str(e)' in seg or '{e}' in seg) and '服务器内部错误' not in seg:
                leaked.append(f"{fname}: {m.group(0)[:80]}")
    check("framework/api 无 500 回显 str(e)/{e} 残留", not leaked, "；".join(leaked[:3]))


def test_500_logger_present():
    """存在 str(e) 的 except 分支应伴生 logger.error 日志"""
    missing = []
    for fname in os.listdir(API_DIR):
        if not fname.endswith('.py'):
            continue
        src = read_api(fname)
        for m in re.finditer(r"except Exception as e:\s*\n(\s+)return jsonify\(.*?500", src, re.S):
            body = m.group(0)
            if 'logger' not in body and 'exc_info' not in body:
                missing.append(fname)
    # 允许有日志的三处业务性 except（auth/plugin_market/framework_update 的已有 logger 分支）
    check("str(e) 500 分支均带 logger 记录", not missing, "；".join(missing[:3]))


# ---- ⑥ ZIP 条目校验 ----

def test_zip_traversal_checks():
    plugins_src = read_api('plugins.py')
    fw_src = read_api('framework_update.py')
    check("plugins 上传校验含反斜杠 \\ 检查",
          "'..' in name or name.startswith('/') or '\\\\' in name" in plugins_src)
    check("framework_update ZIP 校验拒绝 .. / 绝对路径 \\",
          "'..' in name or name.startswith('/') or '\\\\' in name" in fw_src)
    check("framework_update 拒绝符号链接条目",
          "0xA000" in fw_src)


# ---- ⑦ _safe_file_path 符号链接防逃逸 ----

def test_safe_file_path_realpath():
    src = read_api('files.py')
    check("_safe_file_path 使用 os.path.realpath 解析真实路径",
          "os.path.realpath(abs_path)" in src and "os.path.realpath(root_norm)" in src)
    check("校验基于 realpath 后路径而非原始路径",
          "os.path.commonpath([root_real, abs_real])" in src)


if __name__ == '__main__':
    cases = [
        test_db_gateway_sensitive_tables,
        test_files_download_db_blocked_for_admin,
        test_sensitive_ops_require_super,
        test_login_message_unified,
        test_no_500_error_detail_leak,
        test_500_logger_present,
        test_zip_traversal_checks,
        test_safe_file_path_realpath,
    ]
    failed = 0
    for fn in cases:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {fn.__name__} 断言异常: {e}")
    total = sum(1 for fn in cases if fn.__name__ not in ())
    print(f"\n结果: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)