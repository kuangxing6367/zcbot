"""
图片渲染器插件 - 通用图片渲染引擎
提供可复用的图片绘制工具，供其他插件调用生成图片消息。
同时提供 /render_card 命令用于测试渲染效果。

本插件设计为底层渲染库，其他插件可以通过
  from plugins.image_renderer.renderer import render_card, render_text
直接调用，无需重复造轮子。

命令：
  /render_card [标题] [内容]  生成一张信息卡片图片
  /render_text [文字]         将文字渲染为图片

依赖：
  Pillow>=10.0.0
"""
import os
import tempfile
import io
from datetime import datetime

__plugin_meta__ = {
    "name": "图片渲染器",
    "version": "1.0.0",
    "author": "ZGRIC",
    "desc": "通用图片渲染引擎，提供可复用的卡片/文本/图表绘制工具",
    "priority": 200,
}

# 模块级缓存，避免重复加载字体
_FONT_CACHE = {}
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)))


def _get_font(size, bold=False):
    """加载字体（带缓存），找不到则用默认"""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    # 尝试加载插件目录下的字体文件
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
    # 回退默认字体
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
    from PIL import Image, ImageDraw
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

    img = _render_card_image(title, content)
    _send_image(ctx, event, img)


def handle_render_text(event, match):
    """将文字渲染为图片"""
    text = (match.group(1) or event.message or "").strip()
    if not text:
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message="请提供文字，如: /render_text 你好世界",
        )
        return
    img = _render_text_image(text)
    _send_image(ctx, event, img)


def _render_card_image(title, content, width=600, padding=30):
    """
    渲染一张信息卡片图片
    返回 PIL Image 对象
    """
    from PIL import Image, ImageDraw

    # 测量文字尺寸
    title_font = _get_font(28, bold=True)
    content_font = _get_font(20)

    # 先粗略估算高度
    line_height = 30
    content_lines = []
    for line in content.split("\n"):
        if content_font:
            # 估算换行
            avg_char_w = content_font.getlength("中")
            chars_per_line = max(1, int((width - padding * 2) / avg_char_w))
            for i in range(0, len(line), chars_per_line):
                content_lines.append(line[i:i + chars_per_line])
        else:
            content_lines.append(line)

    # 计算总高度
    title_h = 50
    content_h = len(content_lines) * line_height + 20
    footer_h = 30
    total_h = padding * 2 + title_h + content_h + footer_h

    img = Image.new("RGBA", (width, total_h), (255, 255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 绘制渐变背景
    for y in range(total_h):
        ratio = y / total_h
        r = int(248 + ratio * 7)
        g = int(250 + ratio * 5)
        b = int(255 - ratio * 10)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # 标题栏 - 彩色左侧条
    draw.rectangle([padding, padding, padding + 6, padding + title_h], fill=(99, 102, 241))
    if title_font:
        draw.text((padding + 18, padding + 4), title, fill=(20, 30, 60), font=title_font)

    # 内容区域
    y_off = padding + title_h + 10
    if content_font:
        for line in content_lines:
            draw.text((padding + 6, y_off), line, fill=(60, 60, 80), font=content_font)
            y_off += line_height

    # 底部时间戳
    footer_font = _get_font(14)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    if footer_font:
        draw.text((padding, total_h - padding - footer_h + 8), ts, fill=(160, 160, 170), font=footer_font)
        draw.text((width - padding - 80, total_h - padding - footer_h + 8), "ZGRIC", fill=(160, 160, 170), font=footer_font)

    return img


def _render_text_image(text, width=500, padding=20):
    """
    将文字渲染为图片，自动换行
    返回 PIL Image 对象
    """
    from PIL import Image, ImageDraw

    font = _get_font(24)
    line_height = 34

    # 分行
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

    # 半透明背景
    draw.rectangle([0, 0, width - 1, total_h - 1], fill=(248, 250, 255, 255))

    y_off = padding
    if font:
        for line in lines:
            draw.text((padding, y_off), line, fill=(40, 40, 60), font=font)
            y_off += line_height

    return img


def _send_image(ctx, event, img):
    """将 PIL Image 转为文件发送，发送后自动清理"""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            img_path = tmp.name
            img.save(img_path, "PNG")
            del img

        ctx.send_msg(
            user_id=event.user_id,
            group_id=event.group_id,
            message=f"[CQ:image,file=file:///{img_path}]",
        )
    except Exception as e:
        ctx.log(f"发送图片失败: {e}", level="error")
        ctx.send_msg(
            user_id=event.user_id, group_id=event.group_id,
            message=f"图片生成失败: {e}",
        )
    finally:
        try:
            os.unlink(img_path)
        except Exception:
            pass