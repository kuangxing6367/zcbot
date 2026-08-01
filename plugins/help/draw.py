"""
帮助菜单图片渲染器。

从 astrbot_plugin_help.draw.AstrBotHelpDrawer 迁移而来，
去除 astrbot 专属依赖（AstrBotConfig / metadata.yaml / astrbot.api.logger），
改为接收普通 dict 配置，并使用标准库 logging。

内存管理规范：
  - 所有 Image 对象使用完毕后执行 del
  - BytesIO 缓冲区使用 with 语句管理
  - numpy 数组用完即 del
  - 临时图片文件由调用方（main.py）负责删除
"""
import io
import os
import textwrap
from typing import Dict, List, Tuple, Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import logging

logger = logging.getLogger("zgric")


class AstrBotHelpDrawer:
    # ---------------- 常量区 ----------------
    FONT_PATH_REGULAR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "DouyinSansBold.otf"
    )
    FONT_PATH_BOLD = FONT_PATH_REGULAR
    LOGO_PATH = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "astrbot_logo.jpg"
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
    COLOR_LOGO_BG_REMOVE = (255, 255, 255)
    LOGO_BG_TOLERANCE = 25

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
    TALL_CARD_DESC_LENGTH_THRESHOLD = 100

    # 内边距常量
    CARD_PADDING_TOP = 10
    CARD_PADDING_BOTTOM = 10
    NAME_DESC_SPACING = 12

    # ---------------- 构造函数 ----------------
    def __init__(self, config: Dict[str, Any]) -> None:
        """
        :param config: 普通字典配置，支持以下键：
            title_help   - 帮助菜单标题
            title_desc   - 帮助菜单简介
            logo_enable  - 是否启用 logo
            plugin_display_name - 插件显示名（页脚），默认 "Help"
            plugin_version      - 插件版本（页脚），默认 "1.0.0"
            plugin_blacklist    - 插件黑名单列表
            custom_cmds         - 自定义命令列表
        """
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
        self._load_fonts()
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
        if self.logo_enabled:
            return self.TOP_AREA_HEIGHT

        title_h = (
            self.font_title.getbbox(self.title_text)[3]
            - self.font_title.getbbox(self.title_text)[1]
        )
        subtitle_h = (
            self.font_subtitle.getbbox(self.subtitle_text)[3]
            - self.font_subtitle.getbbox(self.subtitle_text)[1]
        )
        computed = (
            self.PADDING + title_h + self.HEADER_TEXT_GAP + subtitle_h + self.PADDING
        )
        return max(self.TOP_AREA_MIN_NO_LOGO, computed)

    # ---------------- 字体 & Logo ----------------
    def _load_fonts(self) -> None:
        try:
            self.font_title = ImageFont.truetype(self.FONT_PATH_BOLD, 36)
            self.font_subtitle = ImageFont.truetype(self.FONT_PATH_REGULAR, 18)
            self.font_plugin_header = ImageFont.truetype(self.FONT_PATH_BOLD, 20)
            self.font_command = ImageFont.truetype(self.FONT_PATH_BOLD, 15)
            self.font_desc = ImageFont.truetype(self.FONT_PATH_REGULAR, 13)
            self.font_footer = ImageFont.truetype(self.FONT_PATH_REGULAR, 12)
        except Exception as e:
            logger.error("加载字体时出错: %s", e)
            raise

    def _load_logo(self) -> None:
        try:
            logo_img = Image.open(self.LOGO_PATH).convert("RGBA")
            img_data = np.array(logo_img)
            r, g, b, a = img_data.T
            white_areas = (
                (r >= self.COLOR_LOGO_BG_REMOVE[0] - self.LOGO_BG_TOLERANCE)
                & (g >= self.COLOR_LOGO_BG_REMOVE[1] - self.LOGO_BG_TOLERANCE)
                & (b >= self.COLOR_LOGO_BG_REMOVE[2] - self.LOGO_BG_TOLERANCE)
                & (a > 128)
            )
            img_data[..., -1][white_areas.T] = 0
            logo_transparent = Image.fromarray(img_data)
            # 释放 numpy 数组
            del img_data, r, g, b, a, white_areas

            ow, oh = logo_transparent.size
            new_w = int(self.LOGO_TARGET_HEIGHT * ow / oh)
            self.resized_logo = logo_transparent.resize(
                (new_w, self.LOGO_TARGET_HEIGHT), Image.Resampling.LANCZOS
            )
            # 缩放完成后释放原始 logo
            del logo_transparent
            del logo_img
        except Exception as e:
            logger.warning("加载或处理 Logo 时出错: %s", e)
            self.resized_logo = None

    # ---------------- 文本解析 ----------------
    @staticmethod
    def _parse_single_command_list(
        text_list,
    ) -> List[Tuple[str, str | None]]:
        """
        将命令列表统一解析为 [(command, desc), ...]。
        支持两种输入格式：
          1. [{"command": str, "desc": str}, ...]
          2. 字符串 / 字符串列表（兼容旧格式）
        """
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
            text_list.strip().splitlines()
            if isinstance(text_list, str)
            else [ln for ln in text_list if ln.strip()]
        )

        for line in lines:
            raw = line
            stripped = line.strip()
            if not stripped or (stripped.startswith("[") and stripped.endswith("]")):
                continue
            if (raw.startswith("  ") or raw.startswith("\t")) and commands:
                cmd, desc = commands[-1]
                commands[-1] = (cmd, (desc or "") + stripped)
                continue

            # 新命令解析
            parts = None
            for sep in (" : ", " # ", "#", ":"):
                if sep in stripped:
                    parts = stripped.split(sep, 1)
                    break
            if parts and len(parts) == 2:
                cmd = (
                    parts[0][2:].strip()
                    if parts[0].startswith("- ")
                    else parts[0].strip()
                )
                desc = parts[1].strip()
            else:
                cmd = (
                    stripped[2:].strip() if stripped.startswith("- ") else stripped
                )
                desc = None
            commands.append((cmd, desc))

        # 只保留描述第一行
        return [
            (c, (d.splitlines()[0].strip() if d else None)) for c, d in commands
        ]

    def _parse_plugin_commands_sorted_grouped(
        self, plugin_dict: Dict[str, Any]
    ) -> List[Tuple[str, List[Tuple[str, str | None]]]]:
        """
        将 {plugin_name: cmds} 解析并排序分组：
          - 命令数 > 1 的插件按命令数降序排列
          - 单命令插件合并到 "简易指令" 分组
          - 追加自定义命令分组（若配置了 custom_cmds）
        """
        large_plugins, small_plugins = [], []
        blacklist = self.config.get("plugin_blacklist", []) or []
        for name, cmds_raw in plugin_dict.items():
            if not cmds_raw:
                continue
            # 黑名单跳过
            if name in blacklist:
                continue
            cmds = self._parse_single_command_list(cmds_raw)
            if not cmds:
                continue
            (small_plugins if len(cmds) == 1 else large_plugins).append(
                (name, cmds)
            )

        large_plugins.sort(key=lambda x: len(x[1]), reverse=True)

        grouped_small_plugin = None
        if small_plugins:
            all_small = [c for _, cmds in small_plugins for c in cmds]
            if all_small:
                grouped_small_plugin = ("简易指令", all_small)
                logger.info("-> 创建 '简易指令' (%d 条)", len(all_small))

        result: List[Tuple[str, List[Tuple[str, str | None]]]] = []
        result.extend(large_plugins)
        if grouped_small_plugin:
            result.append(grouped_small_plugin)

        # 添加自定义命令
        custom_cmds_raw = self.config.get("custom_cmds")
        if custom_cmds_raw:
            custom_list = self._parse_single_command_list(custom_cmds_raw)
            if custom_list:
                result.append(("自定义命令", custom_list))
                logger.info("-> 创建 '自定义命令' (%d 条)", len(custom_list))

        return result

    # ---------------- 绘图辅助 ----------------
    @staticmethod
    def _draw_gradient(
        draw,
        width: int,
        height: int,
        start: Tuple[int, int, int],
        end: Tuple[int, int, int],
    ):
        for y in range(height):
            r = int(start[0] + (end[0] - start[0]) * y / height)
            g = int(start[1] + (end[1] - start[1]) * y / height)
            b = int(start[2] + (end[2] - start[2]) * y / height)
            draw.line([(0, y), (width, y)], fill=(r, g, b))

    def _get_text_metrics(
        self, text: str, font: ImageFont.FreeTypeFont, draw
    ) -> Tuple[Tuple[int, int, int, int], Tuple[int, int]]:
        if not text:
            return (0, 0, 0, 0), (0, 0)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            return bbox, (w, h)
        except AttributeError:
            w = draw.textlength(text, font=font)
            h = (
                sum(font.getmetrics())
                if sum(font.getmetrics()) > 0
                else font.size * 1.2
            )
            return (0, 0, int(w), int(h)), (int(w), int(h))
        except Exception:
            est_w = len(text) * font.size * 0.6
            est_h = font.size * 1.2
            return (0, 0, max(1, int(est_w)), max(1, int(est_h))), (
                max(1, int(est_w)),
                max(1, int(est_h)),
            )

    def _draw_rounded_rectangle(
        self, draw, xy, radius, fill=None, outline=None, width=1
    ):
        x1, y1, x2, y2 = xy
        if x1 >= x2 or y1 >= y2:
            return
        radius = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
        if fill:
            draw.rectangle((x1 + radius, y1, x2 - radius, y2), fill=fill)
            draw.rectangle((x1, y1 + radius, x2, y2 - radius), fill=fill)
            draw.pieslice(
                (x1, y1, x1 + 2 * radius, y1 + 2 * radius), 180, 270, fill=fill
            )
            draw.pieslice(
                (x2 - 2 * radius, y1, x2, y1 + 2 * radius), 270, 360, fill=fill
            )
            draw.pieslice(
                (x1, y2 - 2 * radius, x1 + 2 * radius, y2), 90, 180, fill=fill
            )
            draw.pieslice(
                (x2 - 2 * radius, y2 - 2 * radius, x2, y2), 0, 90, fill=fill
            )
        if outline and width > 0:
            draw.arc(
                (x1, y1, x1 + 2 * radius, y1 + 2 * radius),
                180,
                270,
                fill=outline,
                width=width,
            )
            draw.arc(
                (x2 - 2 * radius, y1, x2, y1 + 2 * radius),
                270,
                360,
                fill=outline,
                width=width,
            )
            draw.arc(
                (x1, y2 - 2 * radius, x1 + 2 * radius, y2),
                90,
                180,
                fill=outline,
                width=width,
            )
            draw.arc(
                (x2 - 2 * radius, y2 - 2 * radius, x2, y2),
                0,
                90,
                fill=outline,
                width=width,
            )
            draw.line(
                [(x1 + radius, y1), (x2 - radius, y1)], fill=outline, width=width
            )
            draw.line(
                [(x1 + radius, y2), (x2 - radius, y2)], fill=outline, width=width
            )
            draw.line(
                [(x1, y1 + radius), (x1, y2 - radius)], fill=outline, width=width
            )
            draw.line(
                [(x2, y1 + radius), (x2, y2 - radius)], fill=outline, width=width
            )

    # ---------------- 卡片布局（每行最多 4 张） ----------------
    def _layout_cards(
        self,
        sections: List[Tuple[str, List[Tuple[str, str | None]]]],
        draw,
    ) -> List[Dict]:
        layout_info: List[Dict] = []
        y_offset = self.top_area_height + self.PADDING
        max_cols = 4
        card_spacing = self.CARD_SPACING
        card_width = (
            self.IMG_WIDTH - self.PADDING * 2 - card_spacing * (max_cols - 1)
        ) // max_cols

        for section_name, cmds in sections:
            # Section Header
            layout_info.append({"type": "header", "name": section_name, "y": y_offset})
            y_offset += self.SECTION_HEADER_HEIGHT + self.SECTION_SPACING_BELOW_HEADER

            row_cards = []
            col_idx = 0
            max_row_height = 0

            for cmd, desc in cmds:
                # 命令文本高度
                _, (w_cmd, h_cmd) = self._get_text_metrics(
                    cmd, self.font_command, draw
                )

                # 自动换行 desc，每行 12 字符
                wrapped_desc = textwrap.wrap(desc or "", width=12)
                bbox = self.font_desc.getbbox("A")
                line_height = bbox[3] - bbox[1] + self.CARD_INTERNAL_SPACE

                # 卡片总高度
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
                    {
                        "type": "card",
                        "name": cmd,
                        "desc": desc,
                        "height": card_h,
                    }
                )
                max_row_height = max(max_row_height, card_h)
                col_idx += 1

                # 达到一行或最后一张
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

            # 剩余不足一行的卡片
            if row_cards:
                for i, card in enumerate(row_cards):
                    card["x"] = self.PADDING + i * (card_width + card_spacing)
                    card["y"] = y_offset
                    card["width"] = card_width
                layout_info.extend(row_cards)
                y_offset += max_row_height + card_spacing

            y_offset += self.SECTION_SPACING_AFTER_CARDS
        return layout_info

    # ---------------- 绘制卡片（每行多张支持） ----------------
    def _draw_cards(self, img: Image.Image, layout_info: List[Dict]) -> None:
        draw = ImageDraw.Draw(img)
        for item in layout_info:
            if item["type"] == "header":
                draw.rectangle(
                    (
                        0,
                        item["y"],
                        self.IMG_WIDTH,
                        item["y"] + self.SECTION_HEADER_HEIGHT,
                    ),
                    fill=self.COLOR_SECTION_HEADER_BG,
                )
                draw.ellipse(
                    (
                        self.SECTION_MARKER_PADDING,
                        item["y"] + self.SECTION_MARKER_PADDING,
                        self.SECTION_MARKER_PADDING + self.SECTION_MARKER_SIZE,
                        item["y"]
                        + self.SECTION_MARKER_PADDING
                        + self.SECTION_MARKER_SIZE,
                    ),
                    fill=self.COLOR_ACCENT,
                )
                draw.text(
                    (
                        self.SECTION_TITLE_LEFT_MARGIN,
                        item["y"] + self.SECTION_MARKER_PADDING,
                    ),
                    item["name"],
                    font=self.font_plugin_header,
                    fill=self.COLOR_TEXT_HEADER,
                )
            elif item["type"] == "card":
                x0, y0 = item["x"], item["y"]
                x1, y1 = x0 + item["width"], y0 + item["height"]
                self._draw_rounded_rectangle(
                    draw,
                    (x0, y0, x1, y1),
                    radius=self.CARD_CORNER_RADIUS,
                    fill=self.COLOR_CARD_BACKGROUND,
                    outline=self.COLOR_CARD_OUTLINE,
                    width=1,
                )
                # name
                draw.text(
                    (x0 + self.CARD_PADDING_X, y0 + self.CARD_PADDING_TOP),
                    item["name"],
                    font=self.font_command,
                    fill=self.COLOR_TEXT_COMMAND,
                )
                if item.get("desc"):
                    wrapped_desc = textwrap.wrap(item["desc"], width=12)
                    bbox_cmd = self.font_command.getbbox(item["name"])
                    y_start = (
                        y0
                        + self.CARD_INTERNAL_SPACE
                        + (bbox_cmd[3] - bbox_cmd[1])
                        + self.NAME_DESC_SPACING
                    )
                    line_height = (
                        self.font_desc.getbbox("A")[3]
                        - self.font_desc.getbbox("A")[1]
                        + self.CARD_INTERNAL_SPACE
                    )
                    for i, line in enumerate(wrapped_desc):
                        draw.text(
                            (x0 + self.CARD_PADDING_X, y_start + i * line_height),
                            line,
                            font=self.font_desc,
                            fill=self.COLOR_TEXT_DESC,
                        )

    # ---------------- 主函数 ----------------
    def draw_help_image(
        self, plugin_commands_dict: Dict[str, List[dict]]
    ) -> bytes:
        """
        生成帮助图片并返回 PNG 字节流。

        :param plugin_commands_dict:
            {plugin_display_name: [{"command": str, "desc": str}, ...]}
            也兼容旧格式（字符串/字符串列表）。
        :return: PNG 图片字节流
        """
        # 统一转换为 Dict[str, List[Tuple[str, str]]] 格式
        # _parse_single_command_list 已支持 dict 列表输入，直接传入即可
        sections = self._parse_plugin_commands_sorted_grouped(plugin_commands_dict)

        if not sections:
            logger.warning("没有可绘制的命令，生成空帮助图片")

        # 初步计算总高度（用临时图片测量）
        temp_img = Image.new("RGB", (self.IMG_WIDTH, 1000), color=(255, 255, 255))
        temp_draw = ImageDraw.Draw(temp_img)
        layout_info = self._layout_cards(sections, temp_draw)

        if layout_info:
            total_height = (
                layout_info[-1]["y"]
                + (layout_info[-1]["height"] if "height" in layout_info[-1] else 0)
                + self.FOOTER_HEIGHT
                + self.PADDING
            )
        else:
            total_height = self.top_area_height + self.FOOTER_HEIGHT + self.PADDING

        # 释放临时测量对象
        del temp_draw
        del temp_img

        # 创建最终图片
        img = Image.new("RGB", (self.IMG_WIDTH, total_height), color=(255, 255, 255))
        draw = ImageDraw.Draw(img)
        # 渐变背景
        self._draw_gradient(
            draw,
            self.IMG_WIDTH,
            total_height,
            self.COLOR_BACKGROUND_START,
            self.COLOR_BACKGROUND_END,
        )

        # 绘制标题与简介（可选 logo）
        title_text = self.title_text
        subtitle_text = self.subtitle_text

        if self.logo_enabled and self.resized_logo:
            img.paste(
                self.resized_logo, (self.PADDING, self.PADDING), self.resized_logo
            )
            logo_w, logo_h = self.resized_logo.size
            x_start = self.PADDING + logo_w + 15
            y_start_title = self.PADDING
        else:
            x_start = self.PADDING
            y_start_title = self.PADDING

        y_start_subtitle = (
            y_start_title
            + self.font_title.getbbox(title_text)[3]
            - self.font_title.getbbox(title_text)[1]
            + self.HEADER_TEXT_GAP
        )
        draw.text(
            (x_start, y_start_title),
            title_text,
            font=self.font_title,
            fill=self.COLOR_TEXT_HEADER,
        )
        draw.text(
            (x_start, y_start_subtitle),
            subtitle_text,
            font=self.font_subtitle,
            fill=self.COLOR_TEXT_SUBTITLE,
        )

        # 绘制卡片
        self._draw_cards(img, layout_info)

        # 底部版权
        footer_text = f"{self.plugin_display_name} v{self.plugin_version}"
        bbox = draw.textbbox((0, 0), footer_text, font=self.font_footer)
        fw = bbox[2] - bbox[0]
        fh = bbox[3] - bbox[1]
        draw.text(
            (
                self.IMG_WIDTH - fw - self.PADDING,
                total_height - self.FOOTER_HEIGHT + (self.FOOTER_HEIGHT - fh) // 2,
            ),
            footer_text,
            font=self.font_footer,
            fill=self.COLOR_TEXT_FOOTER,
        )

        # 释放绘制对象与布局信息
        del draw
        del layout_info

        # 转成 bytes（with 语句管理 BytesIO）
        with io.BytesIO() as output:
            img.save(output, format="PNG", optimize=True)
            data = output.getvalue()

        # 释放最终图片
        del img
        return data
