//! zcbot_render — ZCBOT 原生图片渲染扩展（pyo3 + fontdue + image）
//!
//! 提供两个函数，输出 PNG bytes：
//!   - render_text(text, font_path, width=500, font_size=24, padding=20)
//!   - render_card(title, content, font_path, timestamp, width=600, padding=30)
//!
//! 使用 abi3（稳定 ABI）编译：Windows 产出 zcbot_render.pyd，Linux 产出 zcbot_render.so，
//! 一份扩展兼容 Python 3.9+，由 image_renderer 插件按平台自动加载。

use fontdue::{Font, FontSettings};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;

fn load_font(path: &str) -> Result<Font, String> {
    let data = std::fs::read(path).map_err(|e| format!("读取字体失败: {e}"))?;
    Font::from_bytes(data, FontSettings::default()).map_err(|e| format!("解析字体失败: {e}"))
}

/// 按字符度量宽度换行（CJK 友好），保持与 PIL 版行为一致
fn wrap_lines(font: &Font, text: &str, size: f32, max_w: f32) -> Vec<String> {
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

fn set_px(buf: &mut [u8], width: u32, x: i32, y: i32, c: [u8; 4]) {
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
fn draw_text(
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

fn measure_text(font: &Font, text: &str, size: f32) -> f32 {
    let mut w = 0.0f32;
    for ch in text.chars() {
        let (m, _) = font.rasterize(ch, size);
        w += m.advance_width;
    }
    w
}

fn encode_png(width: u32, height: u32, buf: Vec<u8>) -> Result<Vec<u8>, String> {
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

fn new_buf(width: u32, height: u32, bg: [u8; 4]) -> Vec<u8> {
    let mut buf = vec![0u8; (width * height * 4) as usize];
    for px in buf.chunks_exact_mut(4) {
        px.copy_from_slice(&bg);
    }
    buf
}

/// 将文字渲染为图片，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (text, font_path, width=500, font_size=24, padding=20))]
fn render_text(
    text: &str,
    font_path: &str,
    width: u32,
    font_size: u32,
    padding: u32,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let line_h = (font_size as f32 * 1.35).round().max(1.0) as u32;
    let lines = wrap_lines(&font, text, font_size as f32, (width - padding * 2) as f32);
    let height = padding * 2 + lines.len() as u32 * line_h + 20;
    let mut buf = new_buf(width, height, [248, 250, 255, 255]);
    let mut top = padding as i32;
    for line in &lines {
        draw_text(
            &mut buf,
            width,
            height,
            &font,
            font_size as f32,
            padding as i32,
            top + font_size as i32,
            line,
            [40, 40, 60, 255],
        );
        top += line_h as i32;
    }
    encode_png(width, height, buf).map_err(PyRuntimeError::new_err)
}

/// 渲染信息卡片图片，返回 PNG bytes
#[pyfunction]
#[pyo3(signature = (title, content, font_path, timestamp, width=600, padding=30))]
fn render_card(
    title: &str,
    content: &str,
    font_path: &str,
    timestamp: &str,
    width: u32,
    padding: u32,
) -> PyResult<Vec<u8>> {
    if width == 0 {
        return Err(PyRuntimeError::new_err("width 必须大于 0"));
    }
    let title_size = 28u32;
    let content_size = 20u32;
    let footer_size = 14u32;
    let title_font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let content_font = load_font(font_path).map_err(PyRuntimeError::new_err)?;
    let footer_font = load_font(font_path).map_err(PyRuntimeError::new_err)?;

    let line_h = 30u32;
    let title_h = 50u32;
    let content_lines = wrap_lines(
        &content_font,
        content,
        content_size as f32,
        (width - padding * 2) as f32,
    );
    let content_h = content_lines.len() as u32 * line_h + 20;
    let footer_h = 30u32;
    let total_h = padding * 2 + title_h + content_h + footer_h;

    // 垂直渐变背景（与 PIL 版一致）
    let mut buf = vec![0u8; (width * total_h * 4) as usize];
    for y in 0..total_h {
        let ratio = y as f32 / total_h as f32;
        let c = [
            (248.0 + ratio * 7.0) as u8,
            (250.0 + ratio * 5.0) as u8,
            (255.0 - ratio * 10.0) as u8,
            255,
        ];
        let row = (y * width * 4) as usize;
        for x in 0..width as usize {
            buf[row + x * 4..row + x * 4 + 4].copy_from_slice(&c);
        }
    }

    // 标题栏左侧彩色条
    for y in padding..padding + title_h {
        for x in padding..padding + 6 {
            set_px(&mut buf, width, x as i32, y as i32, [99, 102, 241, 255]);
        }
    }

    // 标题
    draw_text(
        &mut buf,
        width,
        total_h,
        &title_font,
        title_size as f32,
        (padding + 18) as i32,
        (padding + title_size) as i32,
        title,
        [20, 30, 60, 255],
    );

    // 内容
    let mut top = padding + title_h + 10;
    for line in &content_lines {
        draw_text(
            &mut buf,
            width,
            total_h,
            &content_font,
            content_size as f32,
            (padding + 6) as i32,
            (top + content_size) as i32,
            line,
            [60, 60, 80, 255],
        );
        top += line_h;
    }

    // 页脚：左时间戳，右 ZGRIC
    let foot_y = (total_h - padding - footer_h + 8) as i32;
    draw_text(
        &mut buf,
        width,
        total_h,
        &footer_font,
        footer_size as f32,
        padding as i32,
        foot_y + footer_size as i32,
        timestamp,
        [160, 160, 170, 255],
    );
    let z_text = "ZGRIC";
    let z_w = measure_text(&footer_font, z_text, footer_size as f32);
    draw_text(
        &mut buf,
        width,
        total_h,
        &footer_font,
        footer_size as f32,
        (width as i32 - padding as i32 - z_w as i32).max(0),
        foot_y + footer_size as i32,
        z_text,
        [160, 160, 170, 255],
    );

    encode_png(width, total_h, buf).map_err(PyRuntimeError::new_err)
}

#[pymodule]
fn zcbot_render(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(render_text, m)?)?;
    m.add_function(wrap_pyfunction!(render_card, m)?)?;
    Ok(())
}
