//! zcbot_render — ZCBOT 原生图片渲染扩展（pyo3 + fontdue + image）
//!
//! 提供两个函数，输出 PNG bytes：
//!   - render_text(text, font_path, width=500, font_size=24, padding=20, options=None)
//!   - render_card(title, content, font_path, timestamp, width=600, padding=30, options=None)
//!
//! `options` 为可选 dict，与 PIL 回退版参数完全一致，键如下（缺省即默认值）：
//!   颜色（"#RRGGBB" / "#RRGGBBAA" / [r,g,b] / [r,g,b,a]）：
//!     text_color / title_color / content_color / footer_color / accent_color
//!     bg_color（纯色背景） / bg_gradient（[顶部色, 底部色] 垂直渐变，优先于 bg_color）
//!     border_color（边框颜色，默认 None）
//!   字号：font_size / title_size / content_size / footer_size
//!   布局：line_height（0=自动）、padding、align（"left"/"center"/"right"）
//!   样式：radius（圆角像素）、border_width（边框宽，默认 2）
//!   卡片页脚：show_footer（默认 true）、footer_text（默认 "ZGRIC"，None 则不显示右侧文字）
//!
//! 使用 abi3（稳定 ABI）编译：Windows 产出 zcbot_render.pyd，Linux 产出 zcbot_render.so，
//! 一份扩展兼容 Python 3.9+，由 image_renderer 插件按平台自动加载。

use fontdue::{Font, FontSettings};
use image::{GenericImage, GenericImageView};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

pub(crate) mod canvas;

// ---------------------------------------------------------------- 颜色

#[derive(Clone, Copy)]
struct Style {
    text_color: [u8; 4],
    title_color: [u8; 4],
    content_color: [u8; 4],
    footer_color: [u8; 4],
    accent_color: [u8; 4],
    name_color: [u8; 4],
    value_color: [u8; 4],
    highlight_bg: [u8; 4],
    highlight_color: [u8; 4],
    rank_color: [u8; 4],
    bg_color: Option<[u8; 4]>,
    bg_gradient: Option<([u8; 4], [u8; 4])>,
    border_color: Option<[u8; 4]>,
    border_width: u32,
    radius: u32,
    align: Align,
}

impl Default for Style {
    fn default() -> Self {
        Style {
            text_color: [40, 40, 60, 255],
            title_color: [20, 30, 60, 255],
            content_color: [60, 60, 80, 255],
            footer_color: [160, 160, 170, 255],
            accent_color: [99, 102, 241, 255],
            name_color: [40, 40, 60, 255],
            value_color: [120, 120, 140, 255],
            highlight_bg: [236, 239, 255, 255],
            highlight_color: [99, 102, 241, 255],
            rank_color: [160, 160, 170, 255],
            bg_color: None,
            bg_gradient: None,
            border_color: None,
            border_width: 2,
            radius: 0,
            align: Align::Left,
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Align {
    Left,
    Center,
    Right,
}

pub(crate) fn parse_color(obj: &Bound<'_, PyAny>) -> PyResult<Option<[u8; 4]>> {
    if obj.is_none() {
        return Ok(None);
    }
    // 字符串 "#RRGGBB" / "#RRGGBBAA"
    if let Ok(s) = obj.extract::<String>() {
        let hex = s.trim_start_matches('#');
        let len = hex.len();
        let parse = |i: usize| -> PyResult<u8> {
            u8::from_str_radix(&hex[i..i + 2], 16)
                .map_err(|_| PyRuntimeError::new_err(format!("颜色格式非法: {s}")))
        };
        let rgba = match len {
            6 => [parse(0)?, parse(2)?, parse(4)?, 255],
            8 => [parse(0)?, parse(2)?, parse(4)?, parse(6)?],
            _ => return Err(PyRuntimeError::new_err(format!("颜色格式非法: {s}"))),
        };
        return Ok(Some(rgba));
    }
    // 序列 [r,g,b] / [r,g,b,a]
    if let Ok(v) = obj.extract::<Vec<i64>>() {
        let clamp = |n: i64| n.clamp(0, 255) as u8;
        return match v.len() {
            3 => Ok(Some([clamp(v[0]), clamp(v[1]), clamp(v[2]), 255])),
            4 => Ok(Some([clamp(v[0]), clamp(v[1]), clamp(v[2]), clamp(v[3])])),
            _ => Err(PyRuntimeError::new_err("颜色序列长度必须为 3 或 4")),
        };
    }
    Err(PyRuntimeError::new_err(
        "颜色必须是 '#RRGGBB' / '#RRGGBBAA' 或 [r,g,b] / [r,g,b,a]",
    ))
}

fn parse_gradient(obj: &Bound<'_, PyAny>) -> PyResult<Option<([u8; 4], [u8; 4])>> {
    if obj.is_none() {
        return Ok(None);
    }
    let items: Vec<Bound<'_, PyAny>> = obj
        .try_iter()
        .map_err(|_| PyRuntimeError::new_err("bg_gradient 必须是两个颜色的序列"))?
        .collect::<PyResult<_>>()
        .map_err(|_| PyRuntimeError::new_err("bg_gradient 必须是两个颜色的序列"))?;
    if items.len() != 2 {
        return Err(PyRuntimeError::new_err(
            "bg_gradient 必须是两个颜色的序列 [顶部色, 底部色]",
        ));
    }
    let top = parse_color(&items[0])?.ok_or_else(|| PyRuntimeError::new_err("bg_gradient 颜色不能为空"))?;
    let bottom = parse_color(&items[1])?.ok_or_else(|| PyRuntimeError::new_err("bg_gradient 颜色不能为空"))?;
    Ok(Some((top, bottom)))
}

fn parse_align(obj: &Bound<'_, PyAny>) -> PyResult<Align> {
    let s: String = obj
        .extract()
        .map_err(|_| PyRuntimeError::new_err("align 必须是字符串"))?;
    match s.as_str() {
        "left" => Ok(Align::Left),
        "center" => Ok(Align::Center),
        "right" => Ok(Align::Right),
        _ => Err(PyRuntimeError::new_err(
            "align 只能是 'left' / 'center' / 'right'",
        )),
    }
}

// ---------------------------------------------------------------- options 解析

struct Opts {
    font_size: Option<u32>,
    title_size: Option<u32>,
    content_size: Option<u32>,
    footer_size: Option<u32>,
    item_size: Option<u32>,
    line_height: Option<u32>,
    padding: Option<u32>,
    style: Style,
    show_footer: bool,
    footer_text: Option<String>,
}

fn get_opt<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyAny>>> {
    Ok(d.get_item(key)?)
}

fn get_u32(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<u32>> {
    match get_opt(d, key)? {
        Some(v) if !v.is_none() => v
            .extract()
            .map(Some)
            .map_err(|_| PyRuntimeError::new_err(format!("{key} 必须是整数"))),
        _ => Ok(None),
    }
}

fn get_bool(d: &Bound<'_, PyDict>, key: &str, default: bool) -> PyResult<bool> {
    match get_opt(d, key)? {
        Some(v) if !v.is_none() => v
            .extract()
            .map_err(|_| PyRuntimeError::new_err(format!("{key} 必须是布尔值"))),
        _ => Ok(default),
    }
}

fn get_color(d: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<[u8; 4]>> {
    match get_opt(d, key)? {
        Some(v) if !v.is_none() => parse_color(&v),
        _ => Ok(None),
    }
}

impl Opts {
    fn parse(options: Option<&Bound<'_, PyDict>>) -> PyResult<Opts> {
        let mut o = Opts {
            font_size: None,
            title_size: None,
            content_size: None,
            footer_size: None,
            item_size: None,
            line_height: None,
            padding: None,
            style: Style::default(),
            show_footer: true,
            footer_text: None,
        };
        if let Some(d) = options {
            o.font_size = get_u32(d, "font_size")?;
            o.title_size = get_u32(d, "title_size")?;
            o.content_size = get_u32(d, "content_size")?;
            o.footer_size = get_u32(d, "footer_size")?;
            o.item_size = get_u32(d, "item_size")?;
            o.line_height = get_u32(d, "line_height")?;
            o.padding = get_u32(d, "padding")?;
            o.style.bg_color = get_color(d, "bg_color")?;
            if let Some(v) = get_opt(d, "bg_gradient")? {
                if !v.is_none() {
                    o.style.bg_gradient = parse_gradient(&v)?;
                }
            }
            o.style.border_color = get_color(d, "border_color")?;
            o.style.border_width = get_u32(d, "border_width")?.unwrap_or(2);
            o.style.radius = get_u32(d, "radius")?.unwrap_or(0);
            if let Some(v) = get_opt(d, "align")? {
                if !v.is_none() {
                    o.style.align = parse_align(&v)?;
                }
            }
            o.style.text_color = get_color(d, "text_color")?.unwrap_or(o.style.text_color);
            o.style.title_color = get_color(d, "title_color")?.unwrap_or(o.style.title_color);
            o.style.content_color = get_color(d, "content_color")?.unwrap_or(o.style.content_color);
            o.style.footer_color = get_color(d, "footer_color")?.unwrap_or(o.style.footer_color);
            o.style.accent_color = get_color(d, "accent_color")?.unwrap_or(o.style.accent_color);
            o.style.name_color = get_color(d, "name_color")?.unwrap_or(o.style.name_color);
            o.style.value_color = get_color(d, "value_color")?.unwrap_or(o.style.value_color);
            o.style.highlight_bg = get_color(d, "highlight_bg")?.unwrap_or(o.style.highlight_bg);
            o.style.highlight_color = get_color(d, "highlight_color")?.unwrap_or(o.style.highlight_color);
            o.style.rank_color = get_color(d, "rank_color")?.unwrap_or(o.style.rank_color);
            o.show_footer = get_bool(d, "show_footer", true)?;
            o.footer_text = match get_opt(d, "footer_text")? {
                Some(v) if !v.is_none() => Some(
                    v.extract()
                        .map_err(|_| PyRuntimeError::new_err("footer_text 必须是字符串"))?,
                ),
                // 显式 None → 空字符串（隐藏右侧文字）；未传 → None（默认 ZGRIC）
                Some(_) => Some(String::new()),
                None => None,
            };
        }
        Ok(o)
    }
}

// ---------------------------------------------------------------- 字体 & 排版

pub(crate) fn load_font(path: &str) -> Result<Font, String> {
    let data = std::fs::read(path).map_err(|e| format!("读取字体失败: {e}"))?;
    Font::from_bytes(data, FontSettings::default()).map_err(|e| format!("解析字体失败: {e}"))
}

/// 按字符度量宽度换行（CJK 友好），保持与 PIL 版行为一致
pub(crate) fn wrap_lines(font: &Font, text: &str, size: f32, max_w: f32) -> Vec<String> {
    let mut lines: Vec<String> = Vec::new();
    for para in text.split('\n') {
        let mut line = String::new();
        let mut w: f32 = 0.0;
        for ch in para.chars() {
            let (m, _) = font.rasterize(ch, size);
            let cw = m.advance_width;
            if w + cw > max_w && !line.is_empty() {
                lines.push(std::mem::take(&mut line));
                w = 0.0;
            }
            w += cw;
            line.push(ch);
        }
        lines.push(line);
    }
    if lines.is_empty() {
        lines.push(String::new());
    }
    lines
}

pub(crate) fn measure_text(font: &Font, text: &str, size: f32) -> f32 {
    let mut w = 0.0f32;
    for ch in text.chars() {
        let (m, _) = font.rasterize(ch, size);
        w += m.advance_width;
    }
    w
}

pub(crate) fn set_px(buf: &mut [u8], width: u32, x: i32, y: i32, c: [u8; 4]) {
    if x < 0 || y < 0 || x >= width as i32 {
        return;
    }
    let idx = ((y as u32 * width + x as u32) * 4) as usize;
    if idx + 4 <= buf.len() {
        buf[idx..idx + 4].copy_from_slice(&c);
    }
}

/// 在 (baseline_x, baseline_y) 绘制文本，按 glyph 覆盖率做颜色混合
#[allow(clippy::too_many_arguments)]
pub(crate) fn draw_text(
    buf: &mut [u8],
    width: u32,
    height: u32,
    font: &Font,
    size: f32,
    base_x: i32,
    baseline_y: i32,
    text: &str,
    color: [u8; 4],
) {
    let [r, g, b, a] = color;
    let mut cx = base_x;
    for ch in text.chars() {
        let (m, bitmap) = font.rasterize(ch, size);
        for py in 0..m.height as i32 {
            for px in 0..m.width as i32 {
                let cov = bitmap[(py * m.width as i32 + px) as usize] as u32;
                if cov == 0 {
                    continue;
                }
                let bx = cx + m.xmin + px;
                let by = baseline_y + m.ymin + py;
                if bx < 0 || by < 0 || bx >= width as i32 || by >= height as i32 {
                    continue;
                }
                let idx = ((by as u32 * width + bx as u32) * 4) as usize;
                let src_a = (a as u32 * cov) / 255;
                if src_a >= 255 {
                    buf[idx..idx + 4].copy_from_slice(&[r, g, b, 255]);
                } else if src_a > 0 {
                    let inv = 255 - src_a;
                    buf[idx] = ((buf[idx] as u32 * inv + r as u32 * src_a) / 255) as u8;
                    buf[idx + 1] = ((buf[idx + 1] as u32 * inv + g as u32 * src_a) / 255) as u8;
                    buf[idx + 2] = ((buf[idx + 2] as u32 * inv + b as u32 * src_a) / 255) as u8;
                    buf[idx + 3] = 255;
                }
            }
        }
        cx += m.advance_width as i32;
    }
}

/// 计算行在 [x0, x1] 区间内按对齐方式命中的起点
pub(crate) fn line_x(align: Align, line_w: f32, x0: i32, x1: i32) -> i32 {
    let inner = (x1 - x0) as f32;
    match align {
        Align::Left => x0,
        Align::Center => x0 + ((inner - line_w).max(0.0) / 2.0) as i32,
        Align::Right => (x1 as f32 - line_w).max(x0 as f32) as i32,
    }
}

/// 判断像素是否位于 (x0,y0)-(x1,y1) 圆角矩形内（r 为圆角半径）
pub(crate) fn in_rounded(x: i32, y: i32, x0: i32, y0: i32, x1: i32, y1: i32, r: i32) -> bool {
    if x < x0 || y < y0 || x >= x1 || y >= y1 {
        return false;
    }
    if r <= 0 {
        return true;
    }
    let (cx, cy) = if x < x0 + r && y < y0 + r {
        (x0 + r, y0 + r)
    } else if x >= x1 - r && y < y0 + r {
        (x1 - r - 1, y0 + r)
    } else if x < x0 + r && y >= y1 - r {
        (x0 + r, y1 - r - 1)
    } else if x >= x1 - r && y >= y1 - r {
        (x1 - r - 1, y1 - r - 1)
    } else {
        return true;
    };
    let dx = x - cx;
    let dy = y - cy;
    dx * dx + dy * dy <= r * r
}

pub(crate) fn lerp_color(a: [u8; 4], b: [u8; 4], t: f32) -> [u8; 4] {
    let m = |x: u8, y: u8| (x as f32 + (y as f32 - x as f32) * t).round() as u8;
    [m(a[0], b[0]), m(a[1], b[1]), m(a[2], b[2]), 255]
}

/// 填充背景（纯色或垂直渐变），并按圆角裁剪角落为透明
fn fill_bg(buf: &mut [u8], width: u32, height: u32, style: &Style, solid_fallback: [u8; 4]) {
    let w = width as i32;
    let h = height as i32;
    let r = style.radius as i32;
    let solid = style.bg_color;
    let grad = style.bg_gradient;
    for y in 0..h {
        for x in 0..w {
            if !in_rounded(x, y, 0, 0, w, h, r) {
                continue; // 圆角外保持透明
            }
            let c = match (solid, grad) {
                (Some(c), _) => c,
                (None, Some((t, b))) => {
                    let ratio = if h <= 1 { 0.0 } else { y as f32 / (h - 1) as f32 };
                    lerp_color(t, b, ratio)
                }
                (None, None) => solid_fallback,
            };
            set_px(buf, width, x, y, c);
        }
    }
}

/// 绘制圆角矩形边框
fn draw_border(buf: &mut [u8], width: u32, height: u32, style: &Style) {
    let Some(bc) = style.border_color else { return };
    if style.border_width == 0 {
        return;
    }
    let w = width as i32;
    let h = height as i32;
    let r = style.radius as i32;
    let bw = style.border_width as i32;
    for y in 0..h {
        for x in 0..w {
            let outer = in_rounded(x, y, 0, 0, w, h, r);
            if !outer {
                continue;
            }
            let inner_r = (r - bw).max(0);
            let inner = in_rounded(x, y, bw, bw, w - bw, h - bw, inner_r);
            if !inner {
                set_px(buf, width, x, y, bc);
            }
        }
    }
}

pub(crate) fn encode_png(width: u32, height: u32, buf: Vec<u8>) -> Result<Vec<u8>, String> {
    use image::{ImageBuffer, Rgba};
    let img = ImageBuffer::<Rgba<u8>, Vec<u8>>::from_raw(width, height, buf)
        .ok_or_else(|| "图像尺寸非法".to_string())?;
    let mut out: Vec<u8> = Vec::new();
    {
        let mut cursor = std::io::Cursor::new(&mut out);
        image::DynamicImage::ImageRgba8(img)
            .write_to(&mut cursor, image::ImageFormat::Png)
            .map_err(|e| format!("PNG 编码失败: {e}"))?;
    }
    Ok(out)
}

// ---------------------------------------------------------------- 渲染函数

/// 将文字渲染为图片，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (text, font_path, width=500, font_size=24, padding=20, options=None))]
fn render_text(
    text: &str,
    font_path: &str,
    width: u32,
    font_size: u32,
    padding: u32,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let o = Opts::parse(options)?;
    let font_size = o.font_size.unwrap_or(font_size);
    let padding = o.padding.unwrap_or(padding);
    if font_size == 0 {
        return Err(PyRuntimeError::new_err("font_size 必须大于 0"));
    }
    let font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let line_h = match o.line_height {
        Some(v) if v > 0 => v,
        _ => (font_size as f32 * 1.35).round().max(1.0) as u32,
    };
    let inner_w = (width as i32 - padding as i32 * 2).max(1) as f32;
    let lines = wrap_lines(&font, text, font_size as f32, inner_w);
    let height = padding * 2 + lines.len() as u32 * line_h + 20;
    let mut canvas = canvas::Canvas::new_raw(width, height, [0, 0, 0, 0], None)
        .map_err(PyRuntimeError::new_err)?;
    canvas.font = Some(font);
    fill_bg(&mut canvas.buf, width, height, &o.style, [248, 250, 255, 255]);
    draw_border(&mut canvas.buf, width, height, &o.style);
    let mut top = padding as i32;
    for line in &lines {
        let lw = measure_text(canvas.font.as_ref().unwrap(), line, font_size as f32);
        let bx = line_x(
            o.style.align,
            lw,
            padding as i32,
            width as i32 - padding as i32,
        );
        draw_text(
            &mut canvas.buf,
            width,
            height,
            canvas.font.as_ref().unwrap(),
            font_size as f32,
            bx,
            top + font_size as i32,
            line,
            o.style.text_color,
        );
        top += line_h as i32;
    }
    canvas.to_png_bytes().map_err(PyRuntimeError::new_err)
}

/// 渲染信息卡片图片，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (title, content, font_path, timestamp, width=600, padding=30, options=None))]
#[allow(clippy::too_many_arguments)]
fn render_card(
    title: &str,
    content: &str,
    font_path: &str,
    timestamp: &str,
    width: u32,
    padding: u32,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let o = Opts::parse(options)?;
    let padding = o.padding.unwrap_or(padding);
    let title_size = o.title_size.unwrap_or(28);
    let content_size = o.content_size.unwrap_or(20);
    let footer_size = o.footer_size.unwrap_or(14);
    if title_size == 0 || content_size == 0 || footer_size == 0 {
        return Err(PyRuntimeError::new_err("字号必须大于 0"));
    }
    let font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let line_h = match o.line_height {
        Some(v) if v > 0 => v,
        _ => 30u32,
    };
    let title_h = 50u32;
    let inner_w = (width as i32 - padding as i32 * 2).max(1) as f32;
    let content_lines = wrap_lines(&font, content, content_size as f32, inner_w);
    let content_h = content_lines.len() as u32 * line_h + 20;
    let footer_h = if o.show_footer { 30u32 } else { 0u32 };
    let total_h = padding * 2 + title_h + content_h + footer_h;

    let mut canvas = canvas::Canvas::new_raw(width, total_h, [0, 0, 0, 0], None)
        .map_err(PyRuntimeError::new_err)?;
    canvas.font = Some(font);
    // 卡片默认垂直渐变背景（与 PIL 版一致）
    fill_bg(&mut canvas.buf, width, total_h, &o.style, [248, 250, 255, 255]);
    if o.style.bg_color.is_none() && o.style.bg_gradient.is_none() {
        // 覆盖默认渐变：顶部浅蓝 → 底部微黄
        let mut g = o.style;
        g.bg_gradient = Some(([248, 250, 255, 255], [255, 255, 245, 255]));
        fill_bg(&mut canvas.buf, width, total_h, &g, [248, 250, 255, 255]);
    }
    draw_border(&mut canvas.buf, width, total_h, &o.style);

    // 标题栏左侧彩色条
    for y in padding..padding + title_h {
        for x in padding..padding + 6 {
            set_px(&mut canvas.buf, width, x as i32, y as i32, o.style.accent_color);
        }
    }

    // 标题
    draw_text(
        &mut canvas.buf,
        width,
        total_h,
        canvas.font.as_ref().unwrap(),
        title_size as f32,
        (padding + 18) as i32,
        (padding + title_size) as i32,
        title,
        o.style.title_color,
    );

    // 内容（支持对齐，与 PIL 版一致：内容区 x 起点为 padding+6）
    let mut top = padding + title_h + 10;
    let content_x0 = (padding + 6) as i32;
    let content_x1 = width as i32 - padding as i32 - 6;
    for line in &content_lines {
        let lw = measure_text(canvas.font.as_ref().unwrap(), line, content_size as f32);
        let bx = line_x(o.style.align, lw, content_x0, content_x1);
        draw_text(
            &mut canvas.buf,
            width,
            total_h,
            canvas.font.as_ref().unwrap(),
            content_size as f32,
            bx,
            (top + content_size) as i32,
            line,
            o.style.content_color,
        );
        top += line_h;
    }

    // 页脚：左时间戳，右 footer_text（默认 ZGRIC）
    if o.show_footer {
        let foot_y = (total_h - padding - footer_h + 8) as i32;
        draw_text(
            &mut canvas.buf,
            width,
            total_h,
            canvas.font.as_ref().unwrap(),
            footer_size as f32,
            padding as i32,
            foot_y + footer_size as i32,
            timestamp,
            o.style.footer_color,
        );
        let right_text: &str = o.footer_text.as_deref().unwrap_or("ZGRIC");
        if !right_text.is_empty() {
            let z_w = measure_text(canvas.font.as_ref().unwrap(), right_text, footer_size as f32);
            draw_text(
                &mut canvas.buf,
                width,
                total_h,
                canvas.font.as_ref().unwrap(),
                footer_size as f32,
                (width as i32 - padding as i32 - z_w as i32).max(0),
                foot_y + footer_size as i32,
                right_text,
                o.style.footer_color,
            );
        }
    }

    canvas.to_png_bytes().map_err(PyRuntimeError::new_err)
}

/// 渲染榜单/列表图片，返回 PNG bytes
///
/// items 为列表，每项可以是：
///   - 字符串：普通行（左侧文本）
///   - dict：{ "name": 左侧文本, "value": 右侧数值文本, "highlight": 是否高亮,
///             "rank": 可选序号（显示在最左侧）}
#[pyfunction]
#[pyo3(signature = (title, items, font_path, width=600, padding=30, options=None))]
#[allow(clippy::too_many_arguments)]
fn render_list(
    title: &str,
    items: &Bound<'_, PyAny>,
    font_path: &str,
    width: u32,
    padding: u32,
    options: Option<&Bound<'_, PyDict>>,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let o = Opts::parse(options)?;
    let padding = o.padding.unwrap_or(padding);
    let title_size = o.title_size.unwrap_or(24);
    let item_size = o.item_size.unwrap_or(18);
    if title_size == 0 || item_size == 0 {
        return Err(PyRuntimeError::new_err("字号必须大于 0"));
    }
    let font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let line_h = match o.line_height {
        Some(v) if v > 0 => v,
        _ => (item_size as f32 * 1.6).round().max(1.0) as u32,
    };

    // 解析列表项
    struct Item {
        name: String,
        value: String,
        rank: Option<String>,
        highlight: bool,
    }
    let seq: Vec<Bound<'_, PyAny>> = items
        .try_iter()
        .map_err(|_| PyRuntimeError::new_err("items 必须是一个列表"))?
        .collect::<PyResult<_>>()
        .map_err(|_| PyRuntimeError::new_err("items 解析失败"))?;
    let mut parsed: Vec<Item> = Vec::with_capacity(seq.len());
    for it in &seq {
        // 字符串行
        if let Ok(s) = it.extract::<String>() {
            parsed.push(Item {
                name: s,
                value: String::new(),
                rank: None,
                highlight: false,
            });
            continue;
        }
        // dict 行
        let d: &Bound<'_, PyDict> = it
            .downcast()
            .map_err(|_| PyRuntimeError::new_err("列表项必须是字符串或 dict"))?;
        let get_str = |key: &str, default: &str| -> PyResult<String> {
            match d.get_item(key)? {
                Some(v) if !v.is_none() => v
                    .extract()
                    .map_err(|_| PyRuntimeError::new_err(format!("{key} 必须是字符串"))),
                _ => Ok(default.to_string()),
            }
        };
        let name = get_str("name", "")?;
        let value = get_str("value", "")?;
        let rank = match d.get_item("rank")? {
            Some(v) if !v.is_none() => Some(
                v.extract::<String>()
                    .map_err(|_| PyRuntimeError::new_err("rank 必须是字符串或数字"))?,
            ),
            _ => None,
        };
        let highlight = match d.get_item("highlight")? {
            Some(v) if !v.is_none() => v
                .extract::<bool>()
                .map_err(|_| PyRuntimeError::new_err("highlight 必须是布尔值"))?,
            _ => false,
        };
        parsed.push(Item {
            name,
            value,
            rank,
            highlight,
        });
    }

    // 高度：标题区 + 每行 + 底部留白
    let title_h = 56u32;
    let row_h = line_h + 4;
    let total_h = padding * 2 + title_h + parsed.len() as u32 * row_h + 10;
    let mut canvas = canvas::Canvas::new_raw(width, total_h, [0, 0, 0, 0], None)
        .map_err(PyRuntimeError::new_err)?;
    canvas.font = Some(font);

    // 背景（默认同卡片：渐变）
    fill_bg(&mut canvas.buf, width, total_h, &o.style, [248, 250, 255, 255]);
    if o.style.bg_color.is_none() && o.style.bg_gradient.is_none() {
        let mut g = o.style;
        g.bg_gradient = Some(([248, 250, 255, 255], [255, 255, 245, 255]));
        fill_bg(&mut canvas.buf, width, total_h, &g, [248, 250, 255, 255]);
    }
    draw_border(&mut canvas.buf, width, total_h, &o.style);

    // 标题区：左侧彩色条 + 标题
    for y in padding..padding + title_h {
        for x in padding..padding + 6 {
            set_px(&mut canvas.buf, width, x as i32, y as i32, o.style.accent_color);
        }
    }
    draw_text(
        &mut canvas.buf,
        width,
        total_h,
        canvas.font.as_ref().unwrap(),
        title_size as f32,
        (padding + 18) as i32,
        (padding + title_size) as i32,
        title,
        o.style.title_color,
    );

    // 行内容
    let x0 = padding as i32;
    let x1 = width as i32 - padding as i32;
    let rank_w = 44i32; // 序号区宽度
    let mut top = padding + title_h + 8;
    for item in &parsed {
        let row_top = top as i32;
        let row_bot = top + row_h;
        let baseline = row_top + item_size as i32;

        // 高亮整行背景
        if item.highlight {
            for y in row_top..row_bot as i32 {
                for x in x0..x1 {
                    set_px(&mut canvas.buf, width, x, y, o.style.highlight_bg);
                }
            }
        }

        // 序号
        if let Some(r) = &item.rank {
            let rw = measure_text(canvas.font.as_ref().unwrap(), r, item_size as f32);
            draw_text(
                &mut canvas.buf,
                width,
                total_h,
                canvas.font.as_ref().unwrap(),
                item_size as f32,
                (x0 + rank_w) as i32 - rw as i32,
                baseline,
                r,
                o.style.rank_color,
            );
        }

        // 名称（左对齐，跳过序号区）
        let name_x = x0 + rank_w;
        draw_text(
            &mut canvas.buf,
            width,
            total_h,
            canvas.font.as_ref().unwrap(),
            item_size as f32,
            name_x,
            baseline,
            &item.name,
            if item.highlight {
                o.style.highlight_color
            } else {
                o.style.name_color
            },
        );

        // 数值（右对齐）
        if !item.value.is_empty() {
            let vw = measure_text(canvas.font.as_ref().unwrap(), &item.value, item_size as f32);
            draw_text(
                &mut canvas.buf,
                width,
                total_h,
                canvas.font.as_ref().unwrap(),
                item_size as f32,
                (x1 as f32 - vw).max(name_x as f32) as i32,
                baseline,
                &item.value,
                o.style.value_color,
            );
        }

        top += row_h;
    }

    canvas.to_png_bytes().map_err(PyRuntimeError::new_err)
}

// ---------------------------------------------------------------- 图像处理（第二层）

fn decode_img(data: &[u8]) -> Result<image::DynamicImage, String> {
    image::load_from_memory(data).map_err(|e| format!("图片解码失败: {e}"))
}

fn encode_png_img(img: &image::DynamicImage) -> Result<Vec<u8>, String> {
    let mut out: Vec<u8> = Vec::new();
    {
        let mut cursor = std::io::Cursor::new(&mut out);
        img.write_to(&mut cursor, image::ImageFormat::Png)
            .map_err(|e| format!("PNG 编码失败: {e}"))?;
    }
    Ok(out)
}

/// 等比缩放图片（默认保持比例，LANCZOS），返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, width, height, keep_ratio=true))]
fn image_resize(img_bytes: &[u8], width: u32, height: u32, keep_ratio: bool) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    if width == 0 || height == 0 {
        return Err(PyRuntimeError::new_err("width/height 必须大于 0"));
    }
    let (ow, oh) = (img.width(), img.height());
    let (nw, nh) = if keep_ratio {
        let ratio = (width as f32 / ow as f32).min(height as f32 / oh as f32);
        ((ow as f32 * ratio).round().max(1.0) as u32, (oh as f32 * ratio).round().max(1.0) as u32)
    } else {
        (width, height)
    };
    let rgba = img.to_rgba8();
    let resized = image::imageops::resize(&rgba, nw, nh, image::imageops::FilterType::Lanczos3);
    encode_png_img(&image::DynamicImage::ImageRgba8(resized)).map_err(PyRuntimeError::new_err)
}

/// 16:9 居中裁剪，返回 PNG bytes
#[pyfunction]
fn image_crop_16_9(img_bytes: &[u8]) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    let (w, h) = (img.width(), img.height());
    if w == 0 || h == 0 {
        return Err(PyRuntimeError::new_err("图片尺寸非法"));
    }
    let target_h = (w as f32 * 9.0 / 16.0).round() as u32;
    let (crop_h, crop_w) = if target_h <= h {
        (target_h, w)
    } else {
        let target_w = (h as f32 * 16.0 / 9.0).round() as u32;
        (h, target_w)
    };
    let x = (w - crop_w) / 2;
    let y = (h - crop_h) / 2;
    let cropped = img.crop_imm(x, y, crop_w, crop_h);
    encode_png_img(&cropped).map_err(PyRuntimeError::new_err)
}

/// 圆形裁剪（头像），size 为输出边长，四角透明，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, size=256))]
fn image_circle_crop(img_bytes: &[u8], size: u32) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    if size == 0 {
        return Err(PyRuntimeError::new_err("size 必须大于 0"));
    }
    let rgba_in = img.to_rgba8();
    let square = image::imageops::resize(
        &rgba_in,
        size,
        size,
        image::imageops::FilterType::Lanczos3,
    );
    let mut out = image::RgbaImage::new(size, size);
    let r = (size as f32 / 2.0).round() as i32;
    let cx = r;
    let cy = r;
    for y in 0..size {
        for x in 0..size {
            let dx = x as i32 - cx;
            let dy = y as i32 - cy;
            if dx * dx + dy * dy <= r * r {
                out.put_pixel(x, y, *square.get_pixel(x, y));
            }
        }
    }
    encode_png_img(&image::DynamicImage::ImageRgba8(out)).map_err(PyRuntimeError::new_err)
}

/// 圆角裁剪，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, radius=16))]
fn image_round_corners(img_bytes: &[u8], radius: u32) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    let rgba = img.to_rgba8();
    let (w, h) = (rgba.width(), rgba.height());
    let r = (radius as i32).min((w as i32 / 2).max(0)).min((h as i32 / 2).max(0));
    let mut out = rgba.clone();
    for y in 0..h {
        for x in 0..w {
            if !in_rounded(x as i32, y as i32, 0, 0, w as i32, h as i32, r) {
                out.put_pixel(x, y, image::Rgba([0, 0, 0, 0]));
            }
        }
    }
    encode_png_img(&image::DynamicImage::ImageRgba8(out)).map_err(PyRuntimeError::new_err)
}

/// 高斯模糊，radius 为 sigma，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, radius=4.0))]
fn image_blur(img_bytes: &[u8], radius: f32) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    if radius <= 0.0 {
        return encode_png_img(&img).map_err(PyRuntimeError::new_err);
    }
    let blurred = img.blur(radius);
    encode_png_img(&blurred).map_err(PyRuntimeError::new_err)
}

/// 翻转：direction 为 "horizontal" / "vertical"，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, direction="horizontal"))]
fn image_flip(img_bytes: &[u8], direction: &str) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    let out = if direction.eq_ignore_ascii_case("vertical") {
        img.flipv()
    } else {
        img.fliph()
    };
    encode_png_img(&out).map_err(PyRuntimeError::new_err)
}

/// 旋转：angle 为 90/180/270（逆时针），返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, angle=90))]
fn image_rotate(img_bytes: &[u8], angle: i32) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    let out = match angle.rem_euclid(360) {
        90 => img.rotate90(),
        180 => img.rotate180(),
        270 => img.rotate270(),
        _ => img,
    };
    encode_png_img(&out).map_err(PyRuntimeError::new_err)
}

/// 灰度化，返回 PNG bytes
#[pyfunction]
fn image_gray(img_bytes: &[u8]) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    let out = img.grayscale();
    encode_png_img(&out).map_err(PyRuntimeError::new_err)
}

/// 对比度调整，factor > 1 增强 / < 1 减弱，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (img_bytes, factor=1.5))]
fn image_contrast(img_bytes: &[u8], factor: f32) -> PyResult<Vec<u8>> {
    let img = decode_img(img_bytes).map_err(PyRuntimeError::new_err)?;
    let rgba = img.to_rgba8();
    let out = image::imageops::contrast(&rgba, factor);
    encode_png_img(&image::DynamicImage::ImageRgba8(out)).map_err(PyRuntimeError::new_err)
}

/// 将前景图合成到背景图 (x, y) 处（alpha 混合），返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (bg_bytes, fg_bytes, x=0, y=0))]
fn image_overlay(bg_bytes: &[u8], fg_bytes: &[u8], x: i32, y: i32) -> PyResult<Vec<u8>> {
    let bg = decode_img(bg_bytes).map_err(PyRuntimeError::new_err)?.to_rgba8();
    let fg = decode_img(fg_bytes).map_err(PyRuntimeError::new_err)?.to_rgba8();
    let (bw, bh) = (bg.width(), bg.height());
    let (fw, fh) = (fg.width(), fg.height());
    let mut out = bg.clone();
    for j in 0..fh {
        for i in 0..fw {
            let px = x + i as i32;
            let py = y + j as i32;
            if px < 0 || py < 0 || px >= bw as i32 || py >= bh as i32 {
                continue;
            }
            let f = fg.get_pixel(i, j);
            let a = f[3] as u32;
            if a == 0 {
                continue;
            }
            let dst = out.get_pixel(px as u32, py as u32);
            let mut c = [0u8; 4];
            if a >= 255 {
                c = [f[0], f[1], f[2], 255];
            } else {
                let inv = 255 - a;
                for k in 0..3 {
                    c[k] = ((dst[k] as u32 * inv + f[k] as u32 * a) / 255) as u8;
                }
                c[3] = 255;
            }
            out.put_pixel(px as u32, py as u32, image::Rgba(c));
        }
    }
    encode_png_img(&image::DynamicImage::ImageRgba8(out)).map_err(PyRuntimeError::new_err)
}

#[pymodule]
fn zcbot_render(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<canvas::Canvas>()?;
    m.add_function(wrap_pyfunction!(render_text, m)?)?;
    m.add_function(wrap_pyfunction!(render_card, m)?)?;
    m.add_function(wrap_pyfunction!(render_list, m)?)?;
    m.add_function(wrap_pyfunction!(image_resize, m)?)?;
    m.add_function(wrap_pyfunction!(image_crop_16_9, m)?)?;
    m.add_function(wrap_pyfunction!(image_circle_crop, m)?)?;
    m.add_function(wrap_pyfunction!(image_round_corners, m)?)?;
    m.add_function(wrap_pyfunction!(image_blur, m)?)?;
    m.add_function(wrap_pyfunction!(image_flip, m)?)?;
    m.add_function(wrap_pyfunction!(image_rotate, m)?)?;
    m.add_function(wrap_pyfunction!(image_gray, m)?)?;
    m.add_function(wrap_pyfunction!(image_contrast, m)?)?;
    m.add_function(wrap_pyfunction!(image_overlay, m)?)?;
    Ok(())
}
