# -*- coding: utf-8 -*-
"""生成「添加到桌面」用的图标（static/ 下那三个 PNG）

    python _gen_icons.py

★ 为什么要单独生成：图标是**二进制**，手写不出来，改一下就得重跑这个脚本。
   想换图案就改下面的 GLYPH 和 BG，然后重跑。

★ 尺寸不能乱改：
    192×192  安卓主屏用（manifest 里最小的那个，必须有）
    512×512  安卓启动画面 / 应用商店用
    180×180  iOS 的 apple-touch-icon（★ iOS 只认这个尺寸，大了小了都会糊）

★ 安全边距：安卓会把图标裁成圆形或圆角方形，**只有中间约 80% 是安全的**。
   所以图案要缩到 70% 左右，别顶着边画（顶边的话裁完就缺一块）。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'static')

BG = (15, 20, 32, 255)          # #0f1420 —— 跟面板背景一个色
GLYPH = '🤖'                    # 跟面板标题「🤖 TG 机器人管理面板」一致
GLYPH_RATIO = 0.70              # 图案占画布的比例（留出裁切安全区）
FONT_CANDIDATES = [
    r'C:\Windows\Fonts\seguiemj.ttf',       # Windows 的彩色 emoji
    '/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf',   # Linux 的
]

SIZES = [
    ('icon-192.png', 192),
    ('icon-512.png', 512),
    ('apple-touch-icon.png', 180),
]


def pick_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def make(size, font_path):
    img = Image.new('RGBA', (size, size), BG)
    d = ImageDraw.Draw(img)
    if font_path:
        f = ImageFont.truetype(font_path, int(size * GLYPH_RATIO))
        try:
            d.text((size / 2, size / 2), GLYPH, font=f, anchor='mm',
                   embedded_color=True)
            return img
        except Exception as e:
            print('  彩色 emoji 画不出来（%s），退回纯色字形' % e)
    # 退路：画不出 emoji 就写两个字，至少不是空白图
    for cand in (r'C:\Windows\Fonts\msyhbd.ttc',
                 r'C:\Windows\Fonts\arialbd.ttf'):
        if os.path.exists(cand):
            f = ImageFont.truetype(cand, int(size * 0.36))
            d.text((size / 2, size / 2), 'TG', font=f, anchor='mm',
                   fill=(230, 237, 243, 255))
            return img
    raise SystemExit('!! 一个可用字体都没有，生成不了图标')


def main():
    os.makedirs(OUT, exist_ok=True)
    fp = pick_font()
    print('字体：%s' % (fp or '（没有 emoji 字体，走退路）'))
    for name, size in SIZES:
        p = os.path.join(OUT, name)
        img = make(size, fp)
        # ★ iOS 的图标不支持透明通道，存成 RGB 更保险（带 alpha 有的系统会加黑底）
        img.convert('RGB').save(p, 'PNG', optimize=True)
        print('  %-22s %dx%d  %d 字节'
              % (name, size, size, os.path.getsize(p)))
    print('\n图标生成好了 → static/')


if __name__ == '__main__':
    main()
