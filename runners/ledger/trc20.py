from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core import TgError, log


TRON_ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
TRC20_ADDRESS_RE = re.compile(r"(?<![A-Za-z0-9])T[1-9A-HJ-NP-Za-km-z]{33}(?![A-Za-z0-9])")
SHANGHAI_TZ = timezone(timedelta(hours=8))


def is_tron_address(value: str) -> bool:
    """只做地址格式校验；链上转账验证留给后续 API 接入。"""

    return bool(TRON_ADDRESS_RE.fullmatch(value.strip()))


def extract_trc20_address(text: str) -> str | None:
    match = TRC20_ADDRESS_RE.search(text.strip())
    return match.group(0) if match else None


def make_trc20_verify_image(address: str, created_at: datetime | None = None) -> BytesIO:
    created_at = created_at or datetime.now(SHANGHAI_TZ)
    timestamp = created_at.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M:%S")
    width, height = 860, 300
    image = Image.new("RGB", (width, height), "#0aa77c")
    draw = ImageDraw.Draw(image)

    title_font = _font(40, bold=True)
    subtitle_font = _font(20)
    address_font = _font(34, bold=True)
    time_font = _font(20)

    _center_text(draw, (0, 34, width, 80), "USDT防篡改验证核对", title_font, "#fff238")
    _center_text(draw, (0, 78, width, 112), "（请双方谨慎核对地址是否与图中一致，如有误停止付款）", subtitle_font, "#003b30")

    bar = (34, 128, width - 34, 200)
    draw.rounded_rectangle(bar, radius=4, fill="#e87700")
    _center_text(draw, bar, address, address_font, "#ffffff")

    draw.rounded_rectangle((34, 222, width - 34, 268), radius=4, fill="#08936e")
    _center_text(draw, (34, 222, width - 34, 268), f"生成时间：{timestamp}", time_font, "#ffffff")

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    output.name = "trc20-verify.png"
    return output


def reply_trc20_verify_image(api, chat_id, address, reply_to=None):
    """回一张防篡改核对图（面板版：走 api.call_file 上传）

    原版是 PTB 的 reply_photo，这里换成面板的上传接口。
    """
    image = make_trc20_verify_image(address)
    params = {'chat_id': str(chat_id), 'caption': address}
    if reply_to:
        params['reply_to_message_id'] = str(reply_to)
    try:
        return api.call_file('sendPhoto', 'photo', 'trc20-verify.png',
                             image, **params)
    except TgError as e:
        log('发 TRC20 核对图失败：%s' % e)
        return None


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _center_text(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: str,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - width) / 2
    y = box[1] + (box[3] - box[1] - height) / 2 - 2
    draw.text((x, y), text, font=font, fill=fill)
