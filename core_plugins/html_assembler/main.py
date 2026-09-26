# -*- coding: utf-8 -*-
"""
单文件 HTML 装配引擎（官方插件）
对外暴露一组标准接口：赋值、填数据、塞图片 → 输出浏览器能直接打开的单文件 HTML。
引擎只做文本层装配（占位符替换 + 图片 base64 内嵌），不启动浏览器、不执行 JS。
"""
import base64
import importlib.util
import logging
import os
import sys

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "HTML 装配引擎",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "单文件 HTML 装配引擎：赋值/填数据/塞图片 → 输出浏览器直接打开的单文件 HTML（纯标准库）",
    "priority": 200,
    "official": True,
    "process": "host",
}


def _load_engine():
    """同目录加载 engine.py（core 插件以合成模块名装载，不能用常规相对导入）"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'engine.py')
    spec = importlib.util.spec_from_file_location(
        'core_plugin_html_assembler_engine', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['core_plugin_html_assembler_engine'] = mod
    spec.loader.exec_module(mod)
    return mod


engine = _load_engine()

# 模块级门面：其他插件 sys.modules.get('core_plugin_html_assembler') 直用
load_template = engine.load_template
render_html = engine.render_html
render_to_file = engine.render_to_file
embed_image = engine.embed_image
make_assembler = engine.make_assembler
HtmlAssembler = engine.HtmlAssembler


class _HtmlAssemblerService:
    """框架服务门面：fw.services.get('html_assembler') 取到的全局单例"""

    load_template = staticmethod(engine.load_template)
    render_html = staticmethod(engine.render_html)
    render_to_file = staticmethod(engine.render_to_file)
    embed_image = staticmethod(engine.embed_image)
    make_assembler = staticmethod(engine.make_assembler)
    HtmlAssembler = staticmethod(engine.HtmlAssembler)
    engine = engine


_service = None


def register(ctx):
    """注册：配置进引擎、登记框架服务、挂 /html_asm 自测命令"""
    global _service
    fw = ctx._framework

    cfg = fw.config.get('html_assembler', {}) or {}
    eff = engine.configure(cfg)

    # 兼容旧契约：部分用户插件按 plugin_html_assembler 模块名取引擎
    sys.modules.setdefault('plugin_html_assembler', sys.modules[__name__])

    _service = _HtmlAssemblerService()
    fw.services.register('html_assembler', _service)

    ctx.command(
        '/html_asm',
        handle_html_asm,
        priority=200,
        description='HTML装配引擎自测：渲染内置 demo 页并回复输出路径',
    )
    ctx.log("HTML 装配引擎已就绪（上限：模板/图片 "
            f"{eff['max_template_bytes'] // 1024 // 1024}MB / "
            f"{eff['max_image_bytes'] // 1024 // 1024}MB，输出 "
            f"{eff['max_output_bytes'] // 1024 // 1024}MB，图片数 {eff['max_images']}）")


# ── 自测命令 ────────────────────────────────────────────────

# demo 用内嵌 SVG logo（顺带验证 svg 魔数识别 → image/svg+xml）
_DEMO_LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
    '<rect width="96" height="96" rx="18" fill="#2b6cb8"/>'
    '<text x="48" y="62" font-size="44" text-anchor="middle" fill="#fff" '
    'font-family="sans-serif">H</text></svg>'
).encode('utf-8')

_DEMO_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{{ title }}</title>
<style>
body{font-family:'Microsoft YaHei',sans-serif;max-width:560px;margin:40px auto;color:#222}
.card{border:1px solid #e0e0e0;border-radius:12px;padding:28px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
h1{margin:0 0 12px;font-size:22px}
img{vertical-align:middle;margin-right:10px}
.score{font-size:30px;font-weight:bold;color:#2b6cb8}
footer{margin-top:20px;color:#999;font-size:13px}
</style></head><body><div class="card">
<h1><img src="{{ img:logo }}" width="48" alt="logo">{{ title }}</h1>
<p>用户 <b>{{ user }}</b> 的得分是 <span class="score">{{ score }}</span> 分。</p>
{{ raw:footer_html }}
</div></body></html>"""


def handle_html_asm(event, match):
    """渲染内置 demo 页并回复输出路径（覆盖 变量/嵌套/img/raw 全部语法）"""
    out_path = os.path.join(ctx.get_data_dir(), 'html_asm_demo.html')
    a = HtmlAssembler()
    a.set_template(_DEMO_TEMPLATE)
    a.set_many({
        'title': 'HTML 装配引擎自测',
        'user': 'demo',
        'score': 100,
        'footer_html': '<footer>本页由 html_assembler 装配：'
                       '图片已内嵌、无外部引用，双击即可在浏览器打开</footer>',
    })
    a.add_image('logo', _DEMO_LOGO_SVG)
    path = a.render_to_file(out_path)
    st = a.last_stats
    ctx.send_msg(
        user_id=event.user_id, group_id=event.group_id,
        message=f"[html_assembler] demo 已生成：{path}\n"
                f"替换 {st['replaced']} 个占位符 / 内嵌 {st['images']} 张图片 / "
                f"输出 {st['output_bytes']} 字节",
    )


def unregister():
    global _service
    _service = None
