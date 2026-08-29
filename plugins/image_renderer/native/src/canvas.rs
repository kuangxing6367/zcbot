//! Canvas — 链式图元绘制 API（对应 PIL ImageDraw 核心能力）
//!
//! 用法（Python）：
//! ```python
//! c = Canvas.new(600, 400, bg_color="#f8faff", font_path=font)
//! c.rect(20, 20, 580, 380, radius=12, fill="#ffffff", outline="#ccc", width=1)
//! c.text(40, 60, "标题", font_size=28, color="#141e3c")
//! png = c.to_png()
//! ```
//! 所有绘制方法返回 self，可链式调用；最后调用 to_png() 输出。
//!
//! 注意：方法内统一使用 `let canvas = &mut *slf;` 做字段级拆分借用，
//! 避免 PyRefMut 的 DerefMut 导致 buf/width/font 同时借用冲突。

use fontdue::Font;
use image::{imageops, GenericImageView, RgbaImage};
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyAny;

use crate::{
    draw_text, encode_png, in_rounded, lerp_color, line_x, load_font, measure_text, parse_color,
    wrap_lines, Align,
};

/// 将 RGBA 像素混合到目标画布
fn blend_px(buf: &mut [u8], width: u32, x: i32, y: i32, c: [u8; 4]) {
    if x < 0 || y < 0 || x >= width as i32 {
        return;
    }
    let idx = ((y as u32 * width + x as u32) * 4) as usize;
    if idx + 4 > buf.len() {
        return;
    }
    let src_a = c[3] as u32;
    if src_a == 0 {
        return;
    }
    if src_a >= 255 {
        buf[idx..idx + 4].copy_from_slice(&c);
        return;
    }
    let inv = 255 - src_a;
    for i in 0..3 {
        buf[idx + i] = ((buf[idx + i] as u32 * inv + c[i] as u32 * src_a) / 255) as u8;
    }
    buf[idx + 3] = 255;
}

fn draw_line_thick(
    buf: &mut [u8],
    width: u32,
    x0: i32,
    y0: i32,
    x1: i32,
    y1: i32,
    color: [u8; 4],
    thickness: u32,
) {
    let steps = ((x1 - x0).abs().max((y1 - y0).abs())).max(1);
    let t = thickness as i32;
    for i in 0..=steps {
        let t_ = i as f32 / steps as f32;
        let px = x0 + ((x1 - x0) as f32 * t_).round() as i32;
        let py = y0 + ((y1 - y0) as f32 * t_).round() as i32;
        for dy in -t..=t {
            for dx in -t..=t {
                if dx * dx + dy * dy <= t * t {
                    blend_px(buf, width, px + dx, py + dy, color);
                }
            }
        }
    }
}

/// 从 bytes 解码为 RGBA 图片
fn decode_rgba(data: &[u8]) -> Result<RgbaImage, String> {
    image::load_from_memory(data)
        .map(|d| d.to_rgba8())
        .map_err(|e| format!("图片解码失败: {e}"))
}

#[pyclass]
pub(crate) struct Canvas {
    pub(crate) buf: Vec<u8>,
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) font: Option<Font>,
}

impl Canvas {
    /// 内部构造（不经过 Python），供 render_* 组合封装复用
    pub(crate) fn new_raw(
        width: u32,
        height: u32,
        bg: [u8; 4],
        font_path: Option<&str>,
    ) -> Result<Canvas, String> {
        let mut buf = vec![0u8; (width * height * 4) as usize];
        for px in buf.chunks_exact_mut(4) {
            px.copy_from_slice(&bg);
        }
        let font = match font_path {
            Some(p) => Some(load_font(p)?),
            None => None,
        };
        Ok(Canvas {
            buf,
            width,
            height,
            font,
        })
    }

    /// 内部输出 PNG bytes（不经过 Python）
    pub(crate) fn to_png_bytes(&self) -> Result<Vec<u8>, String> {
        encode_png(self.width, self.height, self.buf.clone())
    }
}

fn rgba_image(buf: &[u8], width: u32, height: u32) -> RgbaImage {
    RgbaImage::from_raw(width, height, buf.to_vec()).unwrap_or_else(|| RgbaImage::new(width, height))
}

#[pymethods]
impl Canvas {
    /// 创建画布。bg_color 支持 "#RRGGBB" / "#RRGGBBAA" / [r,g,b,a]，None=透明
    #[new]
    #[pyo3(signature = (width, height, bg_color=None, font_path=None))]
    fn new(
        width: u32,
        height: u32,
        bg_color: Option<&Bound<'_, PyAny>>,
        font_path: Option<&str>,
    ) -> PyResult<Self> {
        if width == 0 || height == 0 {
            return Err(PyRuntimeError::new_err("画布宽高必须大于 0"));
        }
        let bg = match bg_color {
            Some(v) => parse_color(v)?.unwrap_or([0, 0, 0, 0]),
            None => [0, 0, 0, 0],
        };
        let mut buf = vec![0u8; (width * height * 4) as usize];
        for px in buf.chunks_exact_mut(4) {
            px.copy_from_slice(&bg);
        }
        let font = match font_path {
            Some(p) => Some(load_font(p).map_err(PyRuntimeError::new_err)?),
            None => None,
        };
        Ok(Canvas {
            buf,
            width,
            height,
            font,
        })
    }

    fn get_size(&self) -> (u32, u32) {
        (self.width, self.height)
    }

    /// 圆角矩形：fill 填充色 / outline 描边色 / width 描边宽度 / radius 圆角半径
    #[pyo3(signature = (x0, y0, x1, y1, radius=0, fill=None, outline=None, width=1))]
    fn rect<'py>(
        mut slf: PyRefMut<'py, Self>,
        x0: i32,
        y0: i32,
        x1: i32,
        y1: i32,
        radius: u32,
        fill: Option<&Bound<'_, PyAny>>,
        outline: Option<&Bound<'_, PyAny>>,
        width: u32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let canvas = &mut *slf;
        let (w, h) = (canvas.width as i32, canvas.height as i32);
        let x0 = x0.max(0);
        let y0 = y0.max(0);
        let x1 = x1.min(w);
        let y1 = y1.min(h);
        if x1 <= x0 || y1 <= y0 {
            return Ok(slf);
        }
        let radius = radius.min(((x1 - x0) / 2).max(0) as u32).min(((y1 - y0) / 2).max(0) as u32);
        let fill_c = fill.map(|v| parse_color(v)).transpose()?.flatten();
        let outline_c = outline.map(|v| parse_color(v)).transpose()?.flatten();
        let r = radius as i32;
        for y in y0..y1 {
            for x in x0..x1 {
                let inside = in_rounded(x, y, x0, y0, x1, y1, r);
                if !inside {
                    continue;
                }
                if let Some(c) = fill_c {
                    blend_px(&mut canvas.buf, canvas.width, x, y, c);
                }
                if let Some(oc) = outline_c {
                    if width == 0 {
                        continue;
                    }
                    let bw = width as i32;
                    let inner_r = (r - bw).max(0);
                    let inside_inner =
                        in_rounded(x, y, x0 + bw, y0 + bw, x1 - bw, y1 - bw, inner_r);
                    if !inside_inner {
                        blend_px(&mut canvas.buf, canvas.width, x, y, oc);
                    }
                }
            }
        }
        Ok(slf)
    }

    /// 折线：points = [[x,y], ...]，color 描边色，width 线宽
    #[pyo3(signature = (points, color, width=1))]
    fn line<'py>(
        mut slf: PyRefMut<'py, Self>,
        points: Vec<Vec<i64>>,
        color: &Bound<'_, PyAny>,
        width: u32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let c = parse_color(color)?.ok_or_else(|| PyRuntimeError::new_err("line 需要颜色"))?;
        if points.len() < 2 {
            return Ok(slf);
        }
        let canvas = &mut *slf;
        let thickness = width.max(1) - 1;
        for seg in points.windows(2) {
            draw_line_thick(
                &mut canvas.buf,
                canvas.width,
                seg[0][0] as i32,
                seg[0][1] as i32,
                seg[1][0] as i32,
                seg[1][1] as i32,
                c,
                thickness,
            );
        }
        Ok(slf)
    }

    /// 圆形：cx,cy 圆心，r 半径，fill 填充 / outline 描边
    #[pyo3(signature = (cx, cy, r, fill=None, outline=None, width=1))]
    fn circle<'py>(
        mut slf: PyRefMut<'py, Self>,
        cx: i32,
        cy: i32,
        r: i32,
        fill: Option<&Bound<'_, PyAny>>,
        outline: Option<&Bound<'_, PyAny>>,
        width: u32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let fill_c = fill.map(|v| parse_color(v)).transpose()?.flatten();
        let outline_c = outline.map(|v| parse_color(v)).transpose()?.flatten();
        if r <= 0 {
            return Ok(slf);
        }
        let canvas = &mut *slf;
        let bw = width.max(1) as i32;
        for y in (cy - r)..=(cy + r) {
            for x in (cx - r)..=(cx + r) {
                let d2 = (x - cx) * (x - cx) + (y - cy) * (y - cy);
                let r2 = r * r;
                if let Some(c) = fill_c {
                    if d2 <= r2 {
                        blend_px(&mut canvas.buf, canvas.width, x, y, c);
                    }
                }
                if let Some(oc) = outline_c {
                    let inner_r = (r - bw).max(0);
                    if d2 <= r2 && d2 > inner_r * inner_r {
                        blend_px(&mut canvas.buf, canvas.width, x, y, oc);
                    }
                }
            }
        }
        Ok(slf)
    }

    /// 椭圆：x0,y0,x1,y1 外接矩形
    #[pyo3(signature = (x0, y0, x1, y1, fill=None, outline=None, width=1))]
    fn ellipse<'py>(
        mut slf: PyRefMut<'py, Self>,
        x0: i32,
        y0: i32,
        x1: i32,
        y1: i32,
        fill: Option<&Bound<'_, PyAny>>,
        outline: Option<&Bound<'_, PyAny>>,
        width: u32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let fill_c = fill.map(|v| parse_color(v)).transpose()?.flatten();
        let outline_c = outline.map(|v| parse_color(v)).transpose()?.flatten();
        let (cx, cy) = ((x0 + x1) / 2, (y0 + y1) / 2);
        let (rx, ry) = (((x1 - x0) / 2).max(0), ((y1 - y0) / 2).max(0));
        if rx == 0 || ry == 0 {
            return Ok(slf);
        }
        let canvas = &mut *slf;
        let bw = width.max(1) as i32;
        for y in y0..=y1 {
            for x in x0..=x1 {
                let nx = (x - cx) as f32 / rx as f32;
                let ny = (y - cy) as f32 / ry as f32;
                let v = nx * nx + ny * ny;
                if let Some(c) = fill_c {
                    if v <= 1.0 {
                        blend_px(&mut canvas.buf, canvas.width, x, y, c);
                    }
                }
                if let Some(oc) = outline_c {
                    let inner_rx = (rx - bw).max(0) as f32;
                    let inner_ry = (ry - bw).max(0) as f32;
                    let in_inner = if inner_rx > 0.0 && inner_ry > 0.0 {
                        let ax = (x - cx) as f32 / inner_rx;
                        let ay = (y - cy) as f32 / inner_ry;
                        ax * ax + ay * ay <= 1.0
                    } else {
                        false
                    };
                    if v <= 1.0 && !in_inner {
                        blend_px(&mut canvas.buf, canvas.width, x, y, oc);
                    }
                }
            }
        }
        Ok(slf)
    }

    /// 渐变矩形：direction 为 "vertical"(默认) 或 "horizontal"
    #[pyo3(signature = (x0, y0, x1, y1, color_a, color_b, direction="vertical"))]
    fn gradient_rect<'py>(
        mut slf: PyRefMut<'py, Self>,
        x0: i32,
        y0: i32,
        x1: i32,
        y1: i32,
        color_a: &Bound<'_, PyAny>,
        color_b: &Bound<'_, PyAny>,
        direction: &str,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let a = parse_color(color_a)?.unwrap_or([0, 0, 0, 255]);
        let b = parse_color(color_b)?.unwrap_or([0, 0, 0, 255]);
        let (w, h) = (x1 - x0, y1 - y0);
        if w <= 0 || h <= 0 {
            return Ok(slf);
        }
        let canvas = &mut *slf;
        let vertical = !direction.eq_ignore_ascii_case("horizontal");
        for y in y0..y1 {
            for x in x0..x1 {
                let t = if vertical {
                    if h <= 1 {
                        0.0
                    } else {
                        (y - y0) as f32 / (h - 1) as f32
                    }
                } else if w <= 1 {
                    0.0
                } else {
                    (x - x0) as f32 / (w - 1) as f32
                };
                blend_px(&mut canvas.buf, canvas.width, x, y, lerp_color(a, b, t));
            }
        }
        Ok(slf)
    }

    /// 文本：align 为 left/center/right；wrap_width>0 时自动换行
    #[pyo3(signature = (x, y, text, font_size=20, color=None, align="left", wrap_width=0))]
    fn text<'py>(
        mut slf: PyRefMut<'py, Self>,
        x: i32,
        y: i32,
        text: &str,
        font_size: u32,
        color: Option<&Bound<'_, PyAny>>,
        align: &str,
        wrap_width: u32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        if font_size == 0 {
            return Err(PyRuntimeError::new_err("font_size 必须大于 0"));
        }
        let canvas = &mut *slf;
        let font = canvas
            .font
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("未设置字体，请在 Canvas.new 传入 font_path"))?;
        let c = match color {
            Some(v) => parse_color(v)?.unwrap_or([40, 40, 60, 255]),
            None => [40, 40, 60, 255],
        };
        let align = match align {
            "center" => Align::Center,
            "right" => Align::Right,
            _ => Align::Left,
        };
        let size = font_size as f32;
        let lines = if wrap_width > 0 {
            wrap_lines(font, text, size, wrap_width as f32)
        } else {
            text.split('\n').map(|s| s.to_string()).collect()
        };
        let line_h = (size * 1.35).round().max(1.0) as i32;
        let mut baseline = y + font_size as i32;
        for line in &lines {
            let lw = measure_text(font, line, size);
            let bx = if wrap_width > 0 {
                line_x(align, lw, x, x + wrap_width as i32)
            } else {
                line_x(align, lw, x, canvas.width as i32)
            };
            draw_text(
                &mut canvas.buf,
                canvas.width,
                canvas.height,
                font,
                size,
                bx,
                baseline,
                line,
                c,
            );
            baseline += line_h;
        }
        Ok(slf)
    }

    /// 测量文本尺寸（换行后总宽高），供调用方预计算画布高度
    #[pyo3(signature = (text, font_size, wrap_width=0))]
    fn text_metrics(&self, text: &str, font_size: u32, wrap_width: u32) -> PyResult<(u32, u32)> {
        let font = self
            .font
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("未设置字体，请在 Canvas.new 传入 font_path"))?;
        let size = font_size as f32;
        let lines = if wrap_width > 0 {
            wrap_lines(font, text, size, wrap_width as f32)
        } else {
            text.split('\n').map(|s| s.to_string()).collect()
        };
        let mut max_w = 0.0f32;
        for line in &lines {
            max_w = max_w.max(measure_text(font, line, size));
        }
        let line_h = (size * 1.35).round().max(1.0) as u32;
        let h = if lines.is_empty() {
            0
        } else {
            lines.len() as u32 * line_h
        };
        Ok((max_w.round() as u32, h))
    }

    /// 贴图：解码 png/jpeg 后 alpha 混合到画布 (x,y)；width/height 可选缩放
    #[pyo3(signature = (image_bytes, x, y, width=None, height=None))]
    fn paste<'py>(
        mut slf: PyRefMut<'py, Self>,
        image_bytes: &[u8],
        x: i32,
        y: i32,
        width: Option<u32>,
        height: Option<u32>,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let mut img = decode_rgba(image_bytes).map_err(PyRuntimeError::new_err)?;
        if let (Some(nw), Some(nh)) = (width, height) {
            img = imageops::resize(
                &img,
                nw,
                nh,
                image::imageops::FilterType::Lanczos3,
            );
        }
        let canvas = &mut *slf;
        let (iw, ih) = img.dimensions();
        for j in 0..ih {
            for i in 0..iw {
                let p = img.get_pixel(i, j);
                let px = x + i as i32;
                let py = y + j as i32;
                blend_px(&mut canvas.buf, canvas.width, px, py, [p[0], p[1], p[2], p[3]]);
            }
        }
        Ok(slf)
    }

    /// 对整张画布做高斯模糊（radius 为 sigma）
    fn blur<'py>(
        mut slf: PyRefMut<'py, Self>,
        radius: f32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        if radius <= 0.0 {
            return Ok(slf);
        }
        let canvas = &mut *slf;
        let img = rgba_image(&canvas.buf, canvas.width, canvas.height);
        let blurred = imageops::blur(&img, radius);
        canvas.buf = blurred.into_raw();
        Ok(slf)
    }

    /// 半透明遮罩：alpha 0-255
    #[pyo3(signature = (x0, y0, x1, y1, color, alpha))]
    fn alpha_overlay<'py>(
        mut slf: PyRefMut<'py, Self>,
        x0: i32,
        y0: i32,
        x1: i32,
        y1: i32,
        color: &Bound<'_, PyAny>,
        alpha: u32,
    ) -> PyResult<PyRefMut<'py, Self>> {
        let mut c = parse_color(color)?.unwrap_or([0, 0, 0, 255]);
        c[3] = alpha.min(255) as u8;
        let canvas = &mut *slf;
        let (w, h) = (canvas.width as i32, canvas.height as i32);
        for y in y0.max(0)..y1.min(h) {
            for x in x0.max(0)..x1.min(w) {
                blend_px(&mut canvas.buf, canvas.width, x, y, c);
            }
        }
        Ok(slf)
    }

    /// 输出 PNG bytes
    fn to_png(&self) -> PyResult<Vec<u8>> {
        encode_png(self.width, self.height, self.buf.clone()).map_err(PyRuntimeError::new_err)
    }
}
