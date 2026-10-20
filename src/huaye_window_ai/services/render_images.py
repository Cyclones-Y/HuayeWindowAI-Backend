"""生成输入与区域融合；所有 Pillow 运算由调用方放到工作线程中执行。"""

import io
import math
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from huaye_window_ai.core.errors import AppError


def output_dimensions(width: int, height: int) -> tuple[int, int]:
    """固定约 1.5K 像素预算，保留原图比例，避免模型自行选择画布使选区偏移。"""
    if min(width, height) < 15:
        raise AppError("RENDER_IMAGE_UNSUPPORTED", "图片尺寸过小，请重新选择", status_code=422)
    ratio = width / height
    if not 1 / 16 <= ratio <= 16:
        raise AppError("RENDER_IMAGE_UNSUPPORTED", "图片比例过宽或过高，请重新选择", status_code=422)
    return max(16, round(math.sqrt(1536**2 * ratio))), max(16, round(math.sqrt(1536**2 / ratio)))


def open_image(data: bytes) -> Image.Image:
    """解码前检查像素预算，剥离额外信息；供应商成功响应也不能直接信任。"""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 40_000_000:
                raise ValueError("pixel limit")
            source.load()
            normalized = ImageOps.exif_transpose(source)
            image = Image.new("RGB", normalized.size)
            image.paste(normalized.convert("RGB"))
            return image
    except Exception:
        raise AppError("RENDER_IMAGE_INVALID", "生成图片无法读取，请重试", status_code=502) from None


def png(image: Image.Image) -> bytes:
    """无损保存对比图，JPEG 全图重新编码会改变框外像素。"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def selected_box(image: Image.Image, selection: dict[str, Any]) -> tuple[int, int, int, int]:
    """左上向下取整、右下向上取整，最后约束在原图内。"""
    return (
        math.floor(selection["x"] * image.width),
        math.floor(selection["y"] * image.height),
        min(image.width, math.ceil((selection["x"] + selection["width"]) * image.width)),
        min(image.height, math.ceil((selection["y"] + selection["height"]) * image.height)),
    )


def marked_input(data: bytes, selection: dict[str, Any]) -> bytes:
    """额外标记图只供定位，原始输入保留不带框版本以避免红框进入结果。"""
    image = open_image(data)
    ImageDraw.Draw(image).rectangle(selected_box(image, selection), outline="#ff3030", width=max(2, image.width // 250))
    return png(image)


def provider_image(data: bytes) -> bytes:
    """缩至 2048 边长，使无损 PNG 也符合供应商每图 30MB/3600 万像素限制。

    仅模型输入缩小，原始快照和最终对比图仍使用原尺寸；标记图与现场图等比缩放。
    """
    image = open_image(data)
    if min(image.size) < 15:
        raise AppError("RENDER_IMAGE_UNSUPPORTED", "图片尺寸过小，请重新选择", status_code=422)
    image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    return png(image)


def composite_selection(original: bytes, generated: bytes, selection: dict[str, Any]) -> bytes:
    """只贴回选区，羽化完全在框内，保证框外与规范化原图逐像素相同。"""
    image = open_image(original)
    candidate = open_image(generated)
    if abs(candidate.width / candidate.height / (image.width / image.height) - 1) > 0.03:
        raise AppError("RENDER_IMAGE_INVALID", "生成图片比例异常，请重试", status_code=502)
    candidate = candidate.resize(image.size, Image.Resampling.LANCZOS)
    box = selected_box(image, selection)
    width, height = box[2] - box[0], box[3] - box[1]
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    # 极小选区没有可羽化的内部像素，直接贴入；避免构造倒置的 inset 矩形。
    if min(width, height) <= 2:
        image.paste(candidate.crop(box), box[:2])
        return png(image)
    feather = max(1, min(12, width // 20, height // 20, (min(width, height) - 1) // 2))
    for inset in range(feather + 1):
        draw.rectangle((inset, inset, width - 1 - inset, height - 1 - inset), fill=round(255 * inset / feather))
    image.paste(candidate.crop(box), box[:2], mask)
    return png(image)
