"""
图片渲染器插件 - 通用图片渲染引擎
提供可复用的图片绘制工具，供其他插件调用生成图片消息。
同时提供 /render_card 命令用于测试渲染效果。

渲染引擎自动选择（按平台）：
  1. 原生扩展 zcbot_render（Rust + pyo3，Windows .pyd / Linux .so，放在 native/bin/<平台>/）
     → 内存增量 <5MB，无子进程开销
  2. 找不到原生扩展时回退 PIL（Pillow），功能一致

命令：
  /render_card [标题] [内容]  生成一张信息卡片图片
  /render_text [文字]         将文字渲染为图片

依赖：
  Pillow>=10.0.0（PIL 回退用）
"""
import importlib.util
import logging
import os
import sys
import tempfile
from datetime import datetime

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "图片渲染器",
    "version": "1.1.0",
    "author": "ZGRIC",
    "desc": "通用图片渲染引擎（原生 Rust 扩展，自动回退 PIL），提供卡片/文本绘制工具",
    "priority": 200,
}

_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
# 字体候选：插件目录 / help 插件自带字体
_FONT_CANDIDATES = [
    os.path.join(_FONT_DIR, 'HarmonyOS_Sans_SC_Regular.ttf'),
    os.path.join(_FONT_DIR, 'NotoSansCJK-Regular.ttc'),
    os.path.join(os.path.dirname(_FONT_DIR), 'help', 'DouyinSansBold.otf'),
]

# 模块级缓存，避免重复加载字体
_FONT_CACHE = {}


def _load_native_renderer():
    """按平台自动加载原生渲染扩展；找不到返回 None（回退 PIL）"""
    native_dir = os.path.join(_FONT_DIR, 'native', 'bin')
    if sys.platform.startswith('win'):
        subdirs = ['win64', 'win-amd64']
        names = ['zcbot_render.pyd', 'zcbot_render.abi3.pyd']
    elif sys.platform.startswith('linux'):
        subdirs = ['linux-aarch64', 'linux64', 'linux-x86_64']
        names = ['zcbot_render.so', 'zcbot_render.abi3.so']
    else:
        return None
    for sub in subdirs:
        for name in names:
            path = os.path.join(native_dir, sub, name)
            if not os.path.isfile(path):
                continue
            try:
                spec = importlib.util.spec_from_file_location('zcbot_render', path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                logger.info(f"[image_renderer] 原生渲染扩展已加载: {path}")
                return mod
            except Exception as e:
                logger.warning(f"[image_renderer] 原生扩展加载失败，回退 PIL: {path} - {e}")
    return None


_NATIVE = _load_native_renderer()


def _find_font_path():
    """查找可用于原生渲染的字体文件路径"""
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _get_font(size, bold=False):
    """加载字体（带缓存），找不到则用默认（PIL 回退用）"""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    font_names = ["DouyinSansBold.otf", "HarmonyOS_Sans_SC_Regular.ttf", "NotoSansCJK-Regular.ttc"]
    for fn in font_names:
        fp = os.path.join(_FONT_DIR, fn)
        if os.path.isfile(fp):
            try:
                f = ImageFont.truetype(fp, size)
                _FONT_CACHE[key] = f
                return f
            except Exception:
                continue
    try:
        f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f
    except Exception:
        return None


def register(ctx):
    ctx.command(
        "/render_card",
        handle_render_card,
        priority=200,
        description="生成信息卡片图片，用法: /render_card 标题 | 内容",
    )
    ctx.command(
        "/render_text",
        handle_render_text,
        priority=200,
        description="将文字渲染为图片，用法: /render_text 要显示的文字",
    )


def handle_render_card(event, match):
    """生成信息卡片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供内容，如: /render_card 标题 | 内容",
        )
        return
    parts = [p.strip() for p in text.split("|", 1)]
    title = parts[0] if len(parts) > 0 else "信息卡片"
    content = parts[1] if len(parts) > 1 else title

    result = _render_card_image(title, content)
    _send_image(ctx, event, result)


def handle_render_text(event, match):
    """将文字渲染为图片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供文字，如: /render_text 你好世界",
        )
        return
    result = _render_text_image(text)
    _send_image(ctx, event, result)


# ---------------------------------------------------------------- 渲染（原生优先，PIL 回退）

def _render_card_image(title, content, width=600, padding=30):
    """渲染信息卡片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    if _NATIVE is not None:
        font = _find_font_path()
        if font:
            try:
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                return _NATIVE.render_card(title, content, font, ts, width, padding)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_card 失败，回退 PIL: {e}")
    return _render_card_image_pil(title, content, width, padding)


def _render_text_image(text, width=500, padding=20):
    """将文字渲染为图片。原生可用返回 PNG bytes，否则返回 PIL Image"""
    if _NATIVE is not None:
        font = _find_font_path()
        if font:
            try:
                return _NATIVE.render_text(text, font, width, 24, padding)
            except Exception as e:
                logger.warning(f"[image_renderer] 原生 render_text 失败，回退 PIL: {e}")
    return _render_text_image_pil(text, width, padding)


# ---------------------------------------------------------------- PIL 回退实现

def _render_card_image_pil(title, content, width=600, padding=30):
    """PIL 版信息卡片渲染（与原生版布局一致）"""
    from PIL import Image, ImageDraw

    title_font = _get_font(28, bold=True)
    content_font = _get_font(20)

    line_height = 30
    content_lines = []
    for line in content.split("\n"):
        if content_font:
            avg_char_w = content_font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(line), chars_per_line):
                content_lines.append(line[i:i + chars_per_line])
        else:
            content_lines.append(line)

    title_h = 50
    content_h = len(content_lines) * line_height + 20
    footer_h = 30
    total_h = padding * 2 + title_h + content_h + footer_h

    img = Image.new("RGBA", (width, total_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)

    for y in range(total_h):
        ratio = y / total_h
        r = int(248 + ratio * 7)
        g = int(250 + ratio * 5)
        b = int(255 - ratio * 10)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    draw.rectangle([padding, padding, padding + 6, padding + title_h], fill=(99, 102, 241))
    if title_font:
        draw.text((padding + 18, padding + 4), title, fill=(20, 30, 60), font=title_font)

    y_off = padding + title_h + 10
    if content_font:
        for line in content_lines:
            draw.text((padding + 6, y_off), line, fill=(60, 60, 80), font=content_font)
            y_off += line_height

    footer_font = _get_font(14)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    if footer_font:
        draw.text((padding, total_h - padding - footer_h + 8), ts, fill=(160, 160, 170), font=footer_font)
        draw.text((width - padding - 80, total_h - padding - footer_h + 8), "ZGRIC", fill=(160, 160, 170), font=footer_font)

    return img


def _render_text_image_pil(text, width=500, padding=20):
    """PIL 版文字渲染为图片（与原生版布局一致）"""
    from PIL import Image, ImageDraw

    font = _get_font(24)
    line_height = 34

    lines = []
    for para in text.split("\n"):
        if font:
            avg_char_w = font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(para), chars_per_line):
                lines.append(para[i:i + chars_per_line])
        else:
            lines.append(para)

    total_h = padding * 2 + len(lines) * line_height + 20
    img = Image.new("RGBA", (width, total_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width - 1, total_h - 1], fill=(248, 250, 255, 255))

    y_off = padding
    if font:
        for line in lines:
            draw.text((padding, y_off), line, fill=(40, 40, 60), font=font)
            y_off += line_height

    return img


def _send_image(ctx, event, img_or_bytes):
    """发送图片（支持 PIL Image 或 PNG bytes），发送后自动清理"""
    img_path = None
    try:
        is_bytes = isinstance(img_or_bytes, (bytes, bytearray))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img_path = tmp.name
            if is_bytes:
                tmp.write(bytes(img_or_bytes))
            else:
                img_or_bytes.save(tmp.name, "PNG")

        # Windows 临时路径含反斜杠, 需转为正斜杠才能被 OneBot 客户端解析
        path_str = img_path.replace("\\", "/")
        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"[CQ:image,file=file:///{path_str}]",
        )
    except Exception as e:
        ctx.log(f"发送图片失败: {e}", level="error")
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message=f"图片生成失败: {e}",
        )
    finally:
        if img_path:
            try:
                os.unlink(img_path)
            except Exception:
                pass
