# -*- coding: utf-8 -*-
"""
文件浏览器：列目录 / 读写 / 建删改 / 上传下载
"""
import logging
import os
import shutil

from flask import jsonify, request, send_from_directory

logger = logging.getLogger('zcbot')


def register(ctx):
    app = ctx.app
    framework = ctx.framework
    db = ctx.db
    require_auth = ctx.require_auth
    require_super = ctx.require_super
    audit_log = ctx.audit_log
    _project_root = ctx._project_root
    _data_dir = ctx._data_dir

    # ---- 文件浏览器 ----

    _FILE_BROWSER_ALLOWED_ROOTS = []  # 懒初始化

    def _get_file_browser_roots():
        """获取文件浏览器允许访问的根目录列表"""
        if not _FILE_BROWSER_ALLOWED_ROOTS:
            _FILE_BROWSER_ALLOWED_ROOTS.append(_project_root())
            _FILE_BROWSER_ALLOWED_ROOTS.append(framework.plugin_loader.plugins_dir)
            _FILE_BROWSER_ALLOWED_ROOTS.append(framework.plugin_loader.plugins_dat_dir)
            _FILE_BROWSER_ALLOWED_ROOTS.append(_data_dir())
        return _FILE_BROWSER_ALLOWED_ROOTS

    def _safe_file_path(relative_path: str) -> str:
        """将路径解析为绝对路径，并检查是否在允许的根目录内"""
        roots = _get_file_browser_roots()
        # 如果已经是绝对路径，直接规范化
        if os.path.isabs(relative_path):
            abs_path = os.path.normpath(relative_path)
        else:
            abs_path = os.path.normpath(os.path.join(roots[0], relative_path))
        # 检查是否在任意允许的根目录下
        for root in roots:
            root_norm = os.path.normpath(root)
            if os.path.commonpath([root_norm, abs_path]) == root_norm:
                return abs_path
        return None

    @app.route('/api/files/list', methods=['GET'])
    @require_auth
    def file_browser_list():
        """列出指定目录下的文件和子目录"""
        path = request.args.get('path', '').strip()
        if not path:
            # 返回根目录列表
            roots = _get_file_browser_roots()
            return jsonify({'code': 0, 'data': {
                'entries': [
                    {'name': '项目根目录', 'path': _project_root(), 'is_dir': True, 'root': True},
                    {'name': '插件目录', 'path': framework.plugin_loader.plugins_dir, 'is_dir': True, 'root': True},
                    {'name': '插件数据目录', 'path': framework.plugin_loader.plugins_dat_dir, 'is_dir': True, 'root': True},
                    {'name': '数据目录', 'path': _data_dir(), 'is_dir': True, 'root': True},
                ]
            }})
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.isdir(abs_path):
            return jsonify({'code': 400, 'msg': '无效的目录路径'}), 400
        try:
            entries = []
            for name in sorted(os.listdir(abs_path)):
                fpath = os.path.join(abs_path, name)
                is_dir = os.path.isdir(fpath)
                # 跳过隐藏文件和 .venv
                if name.startswith('.') and name != '.':
                    continue
                if name == '.venv' and is_dir:
                    continue
                stat = os.stat(fpath)
                entries.append({
                    'name': name,
                    'path': fpath,
                    'is_dir': is_dir,
                    'size': stat.st_size if not is_dir else 0,
                    'mtime': stat.st_mtime,
                })
            return jsonify({'code': 0, 'data': {
                'current_path': abs_path,
                'entries': entries,
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/read', methods=['GET'])
    @require_auth
    def file_browser_read():
        """读取文件内容"""
        path = request.args.get('path', '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.isfile(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        try:
            ext = os.path.splitext(abs_path)[1].lower()
            binary_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.zip', '.pyc', '.db', '.sqlite'}
            if ext in binary_exts:
                return jsonify({'code': 400, 'msg': '不支持预览二进制文件'}), 400
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
            return jsonify({'code': 0, 'data': {
                'path': abs_path,
                'content': content,
                'size': os.path.getsize(abs_path),
            }})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/write', methods=['PUT'])
    @require_super
    def file_browser_write():
        """写入文件内容（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        content = data.get('content', '')
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path:
            return jsonify({'code': 400, 'msg': '路径不允许'}), 400
        ext = os.path.splitext(abs_path)[1].lower()
        if ext in {'.pyc', '.db', '.sqlite'}:
            return jsonify({'code': 400, 'msg': '不允许写入该类型文件'}), 400
        try:
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(content)
            audit_log(admin['id'], admin['username'], 'file_write', 'file', path,
                      {'size': len(content)})
            return jsonify({'code': 0, 'msg': '文件已保存'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/mkdir', methods=['POST'])
    @require_super
    def file_browser_mkdir():
        """新建目录（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path:
            return jsonify({'code': 400, 'msg': '路径不允许'}), 400
        if os.path.exists(abs_path):
            return jsonify({'code': 400, 'msg': '目录已存在'}), 400
        try:
            os.makedirs(abs_path, exist_ok=True)
            audit_log(admin['id'], admin['username'], 'file_mkdir', 'dir', abs_path)
            return jsonify({'code': 0, 'msg': '目录已创建'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/rename', methods=['POST'])
    @require_super
    def file_browser_rename():
        """重命名文件/目录（仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        new_name = str(data.get('new_name') or '').strip()
        if not path or not new_name:
            return jsonify({'code': 400, 'msg': '缺少 path 或 new_name'}), 400
        if '/' in new_name or '\\' in new_name or new_name in ('.', '..'):
            return jsonify({'code': 400, 'msg': '非法名称'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.exists(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        new_abs = os.path.join(os.path.dirname(abs_path), new_name)
        if not _safe_file_path(new_abs):
            return jsonify({'code': 400, 'msg': '路径不允许'}), 400
        if os.path.exists(new_abs):
            return jsonify({'code': 400, 'msg': '目标已存在'}), 400
        try:
            os.rename(abs_path, new_abs)
            audit_log(admin['id'], admin['username'], 'file_rename', 'file', f"{path} -> {new_name}")
            return jsonify({'code': 0, 'msg': '重命名成功'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/copy', methods=['POST'])
    @require_super
    def file_browser_copy():
        """复制文件/目录到指定目录（自动避重名，仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        src = str(data.get('src') or '').strip()
        dest_dir = str(data.get('dest_dir') or '').strip()
        if not src or not dest_dir:
            return jsonify({'code': 400, 'msg': '缺少 src 或 dest_dir'}), 400
        abs_src = _safe_file_path(src)
        abs_dest = _safe_file_path(dest_dir)
        if not abs_src or not os.path.exists(abs_src):
            return jsonify({'code': 400, 'msg': '源文件不存在'}), 400
        if not abs_dest or not os.path.isdir(abs_dest):
            return jsonify({'code': 400, 'msg': '目标目录不存在'}), 400
        # 禁止把目录复制进自身内部
        if os.path.isdir(abs_src):
            src_real = os.path.realpath(abs_src)
            dest_real = os.path.realpath(abs_dest)
            if dest_real != src_real and os.path.commonpath([src_real, dest_real]) == src_real:
                return jsonify({'code': 400, 'msg': '不能复制到自身内部'}), 400
        name = os.path.basename(abs_src.rstrip('/\\'))
        if not name:
            return jsonify({'code': 400, 'msg': '非法路径'}), 400
        target = os.path.join(abs_dest, name)
        if os.path.exists(target):
            base, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(target):
                target = os.path.join(abs_dest, f"{base}({i}){ext}")
                i += 1
        try:
            if os.path.isdir(abs_src):
                shutil.copytree(abs_src, target)
            else:
                shutil.copy2(abs_src, target)
            audit_log(admin['id'], admin['username'], 'file_copy', 'file',
                      f"{src} -> {target}")
            return jsonify({'code': 0, 'msg': f'已复制为 {os.path.basename(target)}'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/delete', methods=['POST'])
    @require_super
    def file_browser_delete():
        """删除文件/目录（递归，仅超级管理员）"""
        admin = request.admin
        data = request.get_json(silent=True) or {}
        path = str(data.get('path') or '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.exists(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        if os.path.abspath(abs_path) == os.path.normpath(_project_root()):
            return jsonify({'code': 400, 'msg': '禁止删除项目根目录'}), 400
        try:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            else:
                os.remove(abs_path)
            audit_log(admin['id'], admin['username'], 'file_delete', 'file', path)
            return jsonify({'code': 0, 'msg': '已删除'})
        except Exception as e:
            return jsonify({'code': 500, 'msg': str(e)}), 500

    @app.route('/api/files/upload', methods=['POST'])
    @require_super
    def file_browser_upload():
        """上传文件到指定目录（multipart/form-data：字段 dir + 文件列表，仅超级管理员）"""
        admin = request.admin
        target = str(request.form.get('dir') or '').strip()
        if not target:
            return jsonify({'code': 400, 'msg': '缺少 dir'}), 400
        abs_dir = _safe_file_path(target)
        if not abs_dir or not os.path.isdir(abs_dir):
            return jsonify({'code': 400, 'msg': '目标目录不存在'}), 400
        files = request.files.getlist('files')
        if not files:
            return jsonify({'code': 400, 'msg': '未选择文件'}), 400
        saved, failed = [], []
        for f in files:
            name = os.path.basename(f.filename or '')
            if not name or name in ('.', '..'):
                failed.append({'name': f.filename, 'err': '非法文件名'})
                continue
            dest = os.path.join(abs_dir, name)
            if os.path.exists(dest):
                base, ext = os.path.splitext(name)
                i = 1
                while os.path.exists(dest):
                    dest = os.path.join(abs_dir, f"{base}({i}){ext}")
                    i += 1
            try:
                f.save(dest)
                saved.append(os.path.basename(dest))
            except Exception as e:
                failed.append({'name': name, 'err': str(e)})
        if saved:
            audit_log(admin['id'], admin['username'], 'file_upload', 'dir', target,
                      {'saved': saved, 'failed': failed})
        if failed:
            return jsonify({'code': 0, 'msg': f'上传完成：成功 {len(saved)}，失败 {len(failed)}', 'saved': saved}), 200
        return jsonify({'code': 0, 'msg': f'上传成功 {len(saved)} 个文件', 'saved': saved})

    @app.route('/api/files/download', methods=['GET'])
    @require_auth
    def file_browser_download():
        """下载文件"""
        path = request.args.get('path', '').strip()
        if not path:
            return jsonify({'code': 400, 'msg': '缺少 path'}), 400
        abs_path = _safe_file_path(path)
        if not abs_path or not os.path.isfile(abs_path):
            return jsonify({'code': 400, 'msg': '文件不存在'}), 400
        return send_from_directory(
            os.path.dirname(abs_path),
            os.path.basename(abs_path),
            as_attachment=True,
        )
