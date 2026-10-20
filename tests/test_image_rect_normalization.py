"""图片渲染 rect 倒置坐标归一化回归测试。

历史 bug：原生渲染器在倒置坐标（x1<x0 / y1<y0）下静默不画，
PIL 回退在 Pillow 下对倒置坐标抛 ValueError，两条路径行为不一致。
修复后两者都把倒置坐标视为同一矩形，rect(300,200,0,0) == rect(0,0,300,200)。
"""
import importlib.util
import os

import pytest


def _load_ir_module():
    path = os.path.join(
        os.path.dirname(__file__), "..", "core_plugins", "image_renderer", "main.py"
    )
    path = os.path.abspath(path)
    spec = importlib.util.spec_from_file_location("ir_main_rect_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _count_opaque_px(img, target):
    px = img.load()
    w, h = img.size
    n = 0
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a > 200 and (r, g, b) == target:
                n += 1
    return n


def _canvas_pixels(mod, use_native, x0, y0, x1, y1, radius=0):
    if use_native:
        c = mod._get_native_or_pil_canvas(300, 200, (0, 0, 0, 0), None)
        if not hasattr(c, "to_png"):
            # 已回退到 PIL（native 不可用时），与 use_native 期望不符则跳过
            pytest.skip("native 不可用，回退 PIL")
        # 与原生 Canvas 同样的链式调用：先 rect 再 to_png
        c.rect(x0, y0, x1, y1, radius=radius, fill=(255, 0, 0))
        data = c.to_png()
        from PIL import Image
        from io import BytesIO

        return _count_opaque_px(Image.open(BytesIO(data)).convert("RGBA"), (255, 0, 0))
    c = mod._CanvasPIL(300, 200, (0, 0, 0, 0), None)
    c.rect(x0, y0, x1, y1, radius=radius, fill=(255, 0, 0))
    return _count_opaque_px(c._img, (255, 0, 0))


@pytest.mark.skipif(
    importlib.util.find_spec("PIL") is None, reason="Pillow 未安装"
)
def test_pil_rect_inverted_normalized():
    mod = _load_ir_module()
    normal = _canvas_pixels(mod, False, 0, 0, 300, 200, radius=0)
    inverted = _canvas_pixels(mod, False, 300, 200, 0, 0, radius=0)
    assert normal == inverted == 60000


@pytest.mark.skipif(
    importlib.util.find_spec("PIL") is None, reason="Pillow 未安装"
)
def test_pil_rect_inverted_with_radius_no_crash():
    mod = _load_ir_module()
    normal = _canvas_pixels(mod, False, 0, 0, 300, 200, radius=10)
    inverted = _canvas_pixels(mod, False, 300, 200, 0, 0, radius=10)
    assert inverted > 0
    assert abs(normal - inverted) <= 1  # 圆角下像素数应一致


@pytest.mark.skipif(
    importlib.util.find_spec("PIL") is None, reason="Pillow 未安装"
)
def test_native_rect_inverted_normalized():
    mod = _load_ir_module()
    if mod._NATIVE is None:
        pytest.skip("native 渲染器不可用")
    normal = _canvas_pixels(mod, True, 0, 0, 300, 200, radius=0)
    inverted = _canvas_pixels(mod, True, 300, 200, 0, 0, radius=0)
    assert normal == inverted == 60000


@pytest.mark.skipif(
    importlib.util.find_spec("PIL") is None, reason="Pillow 未安装"
)
def test_off_canvas_rect_draws_nothing():
    mod = _load_ir_module()
    off = _canvas_pixels(mod, False, 400, 300, 500, 400, radius=0)
    assert off == 0
