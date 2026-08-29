"""
帮助菜单图片渲染器（Canvas 版，无 PIL）。

帮助菜单图片绘制器，
绘图层改用 image_renderer 的原生 Canvas（Rust），
彻底移除 PIL 绘制依赖。布局常量与计算逻辑保持原版一致。

通过 sys.modules["plugin_image_renderer"] 获取渲染后端
（image_renderer priority=200 最先加载，运行时必然就绪）。
"""
import os
import sys
import textwrap
from typing import Dict, List, Tuple, Any

import logging

logger = logging.getLogger("zgric")


def _img_mod():
    """获取 image_renderer 插件模块（懒获取，避免加载时序问题）"""
    return sys.modules.get("plugin_image_renderer")


class ZcbotHelpDrawer:
    # ---------------- 常量区 ----------------
    FONT_PATH_REGULAR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "DouyinSansBold.otf"
    )
    LOGO_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "zcbot_logo.jpg"
    )

    # 主题色
    COLOR_BACKGROUND_START = (248, 250, 255)
    COLOR_BACKGROUND_END = (255, 252, 248)
    COLOR_SECTION_HEADER_BG = (240, 242, 248)
    COLOR_CARD_BACKGROUND = (255, 255, 255)
    COLOR_CARD_OUTLINE = (220, 225, 235)
    COLOR_TEXT_HEADER = (0, 40, 100)
    COLOR_TEXT_SUBTITLE = (80, 80, 80)
    COLOR_TEXT_PLUGIN = (0, 60, 130)
    COLOR_TEXT_COMMAND = (10, 70, 140)
    COLOR_TEXT_DESC = (70, 70, 70)
    COLOR_TEXT_FOOTER = (100, 100, 100)
    COLOR_ACCENT = (0, 90, 180)

    # 布局尺寸
    IMG_WIDTH = 800
    PADDING = 25
    TOP_AREA_HEIGHT = 120
    TOP_AREA_MIN_NO_LOGO = 80
    HEADER_TEXT_GAP = 5
    LOGO_TARGET_HEIGHT = 65
    SECTION_HEADER_HEIGHT = 50
    SECTION_MARKER_SIZE = 18
    SECTION_MARKER_PADDING = (SECTION_HEADER_HEIGHT - SECTION_MARKER_SIZE) // 2
    SECTION_TITLE_LEFT_MARGIN = SECTION_MARKER_PADDING * 2 + SECTION_MARKER_SIZE
    SECTION_SPACING_BELOW_HEADER = 15
    SECTION_SPACING_AFTER_CARDS = 25
    CARD_PADDING_X = 15
    CARD_PADDING_Y = 12
    CARD_SPACING = 12
    CARD_CORNER_RADIUS = 10
    CARD_INTERNAL_SPACE = 4
    FOOTER_HEIGHT = 40
    CARD_PADDING_TOP = 10
    CARD_PADDING_BOTTOM = 10
    NAME_DESC_SPACING = 12

    # 默认字号（与旧版一致）
    DEFAULT_FONT_SIZES = {
        "title": 30,
        "subtitle": 15,
        "plugin_header": 17,
        "command": 13,
        "desc": 11,
        "footer": 11,
    }

    # ---------------- 构造函数 ----------------
    def __init__(self, config: Dict[str, Any]) -> None:
        if config is None:
            config = {}
        self.config: Dict[str, Any] = config
        self.plugin_display_name = str(
            self.config.get("plugin_display_name") or "Help"
        ).strip()
        self.plugin_version = str(
            self.config.get("plugin_version") or "1.0.0"
        ).strip()
        self.logo_enabled = bool(self.config.get("logo_enable", True))
        self.title_text, self.subtitle_text = self._get_header_texts()
        # 字号表（config 可覆盖）
        sizes = dict(self.DEFAULT_FONT_SIZES)
        sizes.update(self.config.get("font_sizes") or {})
        self.font_sizes = {k: int(v) for k, v in sizes.items()}
        # 字体文件（Canvas 用）
        self.font_path = self.FONT_PATH_REGULAR
        if not os.path.isfile(self.font_path):
            mod = _img_mod()
            self.font_path = (mod._find_font_path() if mod else None) or self.font_path
        self.resized_logo = None
        if self.logo_enabled:
            self._load_logo()
        self.top_area_height = self._calculate_top_area_height()

    def _get_header_texts(self) -> Tuple[str, str]:
        title_text = str(self.config.get("title_help", "") or "").strip() or "帮助菜单"
        subtitle_text = (
            str(self.config.get("title_desc", "") or "").strip()
            or "可用插件及指令列表"
        )
        return title_text, subtitle_text

    def _calculate_top_area_height(self) -> int:
        """估算顶部区域高度（字号*1.35 行高，与原版 getbbox 高度一致）"""
        title_h = int(self.font_sizes["title"] * 1.35)
        subtitle_h = int(self.font_sizes["subtitle"] * 1.35)
        computed = self.PADDING + title_h + self.HEADER_TEXT_GAP + subtitle_h + self.PADDING
        return max(self.TOP_AREA_MIN_NO_LOGO, computed)

    # ---------------- Logo ----------------
    def _load_logo(self) -> None:
        """Logo 转 bytes 并等比缩放到目标高度（image_renderer.image_resize）"""
        if not os.path.isfile(self.LOGO_PATH):
            self.resized_logo = None
            return
        mod = _img_mod()
        if mod is None or not hasattr(mod, "image_resize"):
            self.resized_logo = None
            return
        try:
            with open(self.LOGO_PATH, "rb") as f:
                raw = f.read()
            # 等比缩放到高度 65（宽按比例）
            self.resized_logo = mod.image_resize(
                raw, 9999, self.LOGO_TARGET_HEIGHT, keep_ratio=True
            )
        except Exception as e:
            logger.warning("加载或处理 Logo 时出错: %s", e)
            self.resized_logo = None

    # ---------------- 文本解析 ----------------
    @staticmethod
    def _parse_single_command_list(text_list) -> List[Tuple[str, str | None]]:
        if (
            isinstance(text_list, list)
            and text_list
            and all(isinstance(item, dict) for item in text_list)
        ):
            commands = []
            for item in text_list:
                cmd = str(item.get("command") or "").strip()
                if not cmd:
                    continue
                desc_raw = item.get("desc")
                desc = str(desc_raw).strip() if desc_raw else None
                commands.append(
                    (cmd, desc.splitlines()[0].strip() if desc else None)
                )
            return commands

        commands = []
        lines = (
            [str(text_list)]
            if isinstance(text_list, str)
            else (list(text_list) if text_list else [])
        )
        for line in lines:
            line = str(line).strip()
            if not line:
                continue
            if " " in line:
                cmd, _, desc = line.partition(" ")
                commands.append((cmd.strip(), desc.strip() or None))
            else:
                commands.append((line, None))
        return commands

    def _parse_plugin_commands_sorted_grouped(
        self, plugin_commands_dict: Dict[str, List[dict]]
    ) -> List[Tuple[str, List[Tuple[str, str | None]]]]:
        """按插件名排序分组，返回 [(plugin_name, [(cmd, desc), ...])]"""
        sections = []
        names = sorted(plugin_commands_dict.keys())
        for name in names:
            cmds = self._parse_single_command_list(plugin_commands_dict.get(name))
            sections.append((name, cmds))
        return sections

    # ---------------- 文本测量 ----------------
    def _measure_text(self, canvas, text: str, size: int) -> Tuple[int, int]:
        """测量文本宽高（Canvas.text_metrics 或估算兜底）"""
        if not text:
            return (0, 0)
        try:
            w, h = canvas.text_metrics(text, int(size))
            return (int(w), int(h))
        except Exception:
            return (max(1, len(text) * size // 2), int(size * 1.35))

    # ---------------- 卡片布局（每行最多 4 张） ----------------
    def _layout_cards(self, sections, canvas) -> List[Dict]:
        layout_info: List[Dict] = []
        y_offset = self.top_area_height + self.PADDING
        max_cols = 4
        card_spacing = self.CARD_SPACING
        card_width = (
            self.IMG_WIDTH - self.PADDING * 2 - card_spacing * (max_cols - 1)
        ) // max_cols
        cmd_size = self.font_sizes["command"]
        desc_size = self.font_sizes["desc"]
        line_height = int(desc_size * 1.35) + self.CARD_INTERNAL_SPACE

        for section_name, cmds in sections:
            layout_info.append({"type": "header", "name": section_name, "y": y_offset})
            y_offset += self.SECTION_HEADER_HEIGHT + self.SECTION_SPACING_BELOW_HEADER

            row_cards = []
            col_idx = 0
            max_row_height = 0

            for cmd, desc in cmds:
                _, h_cmd = self._measure_text(canvas, cmd, cmd_size)
                wrapped_desc = textwrap.wrap(desc or "", width=12)
                h_desc_total = len(wrapped_desc) * line_height if wrapped_desc else 0
                card_h = max(
                    self.CARD_PADDING_TOP
                    + h_cmd
                    + self.NAME_DESC_SPACING
                    + h_desc_total
                    + self.CARD_PADDING_BOTTOM,
                    35,
                )
                row_cards.append(
                    {"type": "card", "name": cmd, "desc": desc, "height": card_h}
                )
                max_row_height = max(max_row_height, card_h)
                col_idx += 1

                if col_idx == max_cols:
                    for i, card in enumerate(row_cards):
                        card["x"] = self.PADDING + i * (card_width + card_spacing)
                        card["y"] = y_offset
                        card["width"] = card_width
                    layout_info.extend(row_cards)
                    y_offset += max_row_height + card_spacing
                    row_cards = []
                    col_idx = 0
                    max_row_height = 0

            if row_cards:
                for i, card in enumerate(row_cards):
                    card["x"] = self.PADDING + i * (card_width + card_spacing)
                    card["y"] = y_offset
                    card["width"] = card_width
                layout_info.extend(row_cards)
                y_offset += max_row_height + card_spacing

            y_offset += self.SECTION_SPACING_AFTER_CARDS
        return layout_info

    # ---------------- 绘制 ----------------
    def _draw_cards(self, canvas, layout_info: List[Dict]) -> None:
        cmd_size = self.font_sizes["command"]
        desc_size = self.font_sizes["desc"]
        line_height = int(desc_size * 1.35) + self.CARD_INTERNAL_SPACE

        for item in layout_info:
            if item["type"] == "header":
                y = item["y"]
                # 分区背景条
                canvas.rect(
                    0, y, self.IMG_WIDTH, y + self.SECTION_HEADER_HEIGHT,
                    radius=0, fill=self.COLOR_SECTION_HEADER_BG,
                )
                # 左侧彩条（圆点 marker）
                canvas.circle(
                    self.SECTION_MARKER_PADDING + self.SECTION_MARKER_SIZE // 2,
                    y + self.SECTION_MARKER_PADDING + self.SECTION_MARKER_SIZE // 2,
                    self.SECTION_MARKER_SIZE // 2,
                    fill=self.COLOR_ACCENT,
                )
                canvas.text(
                    self.SECTION_TITLE_LEFT_MARGIN,
                    y + self.SECTION_MARKER_PADDING,
                    item["name"],
                    font_size=self.font_sizes["plugin_header"],
                    color=self.COLOR_TEXT_HEADER,
                )
            elif item["type"] == "card":
                x0, y0 = item["x"], item["y"]
                x1, y1 = x0 + item["width"], y0 + item["height"]
                # 圆角卡片（填充+描边）
                canvas.rect(
                    x0, y0, x1, y1,
                    radius=self.CARD_CORNER_RADIUS,
                    fill=self.COLOR_CARD_BACKGROUND,
                    outline=self.COLOR_CARD_OUTLINE,
                    width=1,
                )
                canvas.text(
                    x0 + self.CARD_PADDING_X,
                    y0 + self.CARD_PADDING_TOP,
                    item["name"],
                    font_size=cmd_size,
                    color=self.COLOR_TEXT_COMMAND,
                )
                if item.get("desc"):
                    wrapped_desc = textwrap.wrap(item["desc"], width=12)
                    _, h_cmd = self._measure_text(canvas, item["name"], cmd_size)
                    y_start = y0 + self.CARD_INTERNAL_SPACE + h_cmd + self.NAME_DESC_SPACING
                    for i, line in enumerate(wrapped_desc):
                        canvas.text(
                            x0 + self.CARD_PADDING_X,
                            y_start + i * line_height,
                            line,
                            font_size=desc_size,
                            color=self.COLOR_TEXT_DESC,
                        )

    # ---------------- 主函数 ----------------
    def draw_help_image(self, plugin_commands_dict: Dict[str, List[dict]]) -> bytes:
        """生成帮助图片并返回 PNG 字节流（Canvas 渲染）"""
        mod = _img_mod()
        if mod is None or not hasattr(mod, "_get_native_or_pil_canvas"):
            raise RuntimeError("image_renderer 未加载，无法渲染帮助图片")

        sections = self._parse_plugin_commands_sorted_grouped(plugin_commands_dict)
        if not sections:
            logger.warning("没有可绘制的命令，生成空帮助图片")

        # 用临时画布测量布局
        probe = mod._get_native_or_pil_canvas(
            self.IMG_WIDTH, 1000, None, self.font_path
        )
        layout_info = self._layout_cards(sections, probe)
        try:
            del probe
        except Exception:
            pass

        if layout_info:
            total_height = (
                layout_info[-1]["y"]
                + (layout_info[-1]["height"] if "height" in layout_info[-1] else 0)
                + self.FOOTER_HEIGHT
                + self.PADDING
            )
        else:
            total_height = self.top_area_height + self.FOOTER_HEIGHT + self.PADDING

        # 创建最终画布
        canvas = mod._get_native_or_pil_canvas(
            self.IMG_WIDTH, total_height, None, self.font_path
        )
        try:
            # 渐变背景
            canvas.gradient_rect(
                0, 0, self.IMG_WIDTH, total_height,
                self.COLOR_BACKGROUND_START, self.COLOR_BACKGROUND_END,
                direction="vertical",
            )

            # 标题与简介（可选 logo）
            if self.resized_logo:
                canvas.paste(
                    self.resized_logo, self.PADDING, self.PADDING
                )
                x_start = self.PADDING + 15
                # logo 宽按高度 65 估算（image_resize 已等比，宽≈65*原始宽高比，此处按 1.5 倍估）
                x_start = self.PADDING + 65 * 2 + 15
            else:
                x_start = self.PADDING

            y_start_title = self.PADDING
            y_start_subtitle = (
                y_start_title
                + int(self.font_sizes["title"] * 1.35)
                + self.HEADER_TEXT_GAP
            )
            canvas.text(
                x_start, y_start_title, self.title_text,
                font_size=self.font_sizes["title"], color=self.COLOR_TEXT_HEADER,
            )
            canvas.text(
                x_start, y_start_subtitle, self.subtitle_text,
                font_size=self.font_sizes["subtitle"], color=self.COLOR_TEXT_SUBTITLE,
            )

            # 卡片
            self._draw_cards(canvas, layout_info)

            # 底部版权
            footer_text = f"{self.plugin_display_name} v{self.plugin_version}"
            fw, _ = self._measure_text(canvas, footer_text, self.font_sizes["footer"])
            canvas.text(
                self.IMG_WIDTH - fw - self.PADDING,
                total_height - self.FOOTER_HEIGHT + (self.FOOTER_HEIGHT - int(self.font_sizes["footer"] * 1.35)) // 2,
                footer_text,
                font_size=self.font_sizes["footer"],
                color=self.COLOR_TEXT_FOOTER,
            )

            del layout_info
            return bytes(canvas.to_png())
        finally:
            try:
                del canvas
            except Exception:
                pass
