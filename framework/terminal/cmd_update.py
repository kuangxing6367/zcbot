# -*- coding: utf-8 -*-
"""
终端命令：框架源码更新（update）
"""
import os

from .command import terminal_commands


def register(fw):
    """注册本组终端命令"""
    def cmd_update(args):
        """更新框架: update [版本号]"""
        import requests, zipfile, tempfile, shutil
        repo = 'kuangxing6367/zcbot'
        branch = 'main'
        target = args.strip() or ''

        # 读取本地版本
        try:
            local_ver = open('VERSION', 'r', encoding='utf-8').read().strip()
        except Exception:
            local_ver = '0.0.0'

        # 确定目标版本
        if target:
            tag = f'v{target}' if not target.startswith('v') else target
            zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
            print(f"正在下载框架更新（{tag}）...")
        else:
            # 取最新 Release
            try:
                r = requests.get(f"https://api.github.com/repos/{repo}/releases?per_page=5", timeout=15)
                releases = r.json() if r.status_code == 200 else []
                best = None
                for rel in releases:
                    t = rel.get('tag_name', '')
                    if best is None or t > best:
                        best = t
                if best:
                    tag = best
                    zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
                    print(f"正在下载框架更新（最新 Release {tag}）...")
                else:
                    tag = ''
                    zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
                    print("仓库无 Release，下载 main 分支最新代码...")
            except Exception:
                tag = ''
                zip_url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
                print("获取版本信息失败，下载 main 分支最新代码...")

        # 下载
        try:
            resp = requests.get(zip_url, timeout=60)
            if resp.status_code != 200:
                print(f"下载失败: HTTP {resp.status_code}")
                return
        except Exception as e:
            print(f"下载失败: {e}")
            return

        # 解压并覆盖
        tmp_zip = tempfile.mktemp(suffix='.zip')
        try:
            with open(tmp_zip, 'wb') as f:
                f.write(resp.content)
            tmp_dir = tempfile.mkdtemp(prefix='zcbot_upd_')
            try:
                with zipfile.ZipFile(tmp_zip, 'r') as zf:
                    zf.extractall(tmp_dir)
                entries = [e for e in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, e))]
                src_root = os.path.join(tmp_dir, entries[0]) if entries[0] else tmp_dir

                root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                include = {'framework', 'core_plugins', 'web', 'webui', 'sql', 'main.py', 'requirements.txt', 'VERSION', 'CHANGELOG.md', 'README.md', 'start.sh'}
                updated = []
                for name in os.listdir(src_root):
                    if name not in include:
                        continue
                    src = os.path.join(src_root, name)
                    dst = os.path.join(root, name)
                    if os.path.isdir(src):
                        if os.path.isdir(dst):
                            shutil.rmtree(dst, ignore_errors=True)
                        shutil.copytree(src, dst)
                    elif os.path.isfile(src):
                        os.makedirs(os.path.dirname(dst), exist_ok=True) if os.path.dirname(dst) else None
                        shutil.copy2(src, dst)
                    updated.append(name)

                print(f"更新完成！共更新 {len(updated)} 项: {', '.join(updated)}")
                print("请重启框架生效: python main.py")
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as e:
            print(f"更新失败: {e}")
        finally:
            try:
                os.unlink(tmp_zip)
            except Exception:
                pass



    # ---- 注册 ----
    terminal_commands.register("update", cmd_update, "更新框架: update [版本号]")

