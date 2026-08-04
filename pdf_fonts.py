#!/usr/bin/env python3
"""PDF 中文字体注册公共模块 — 统一多候选回退链。

原先 gen_report_pdf.py / gen_report_clean.py / gen_verify_pdf.py / gen_pred_pdf.py
各有一份几乎相同的 register_cjk_font 实现, 此处抽取为公共函数, 消除冗余。

用法:
    from pdf_fonts import register_cjk_font
    register_cjk_font()                      # 注册 CJK / CJK-Bold
    register_cjk_font(bold_name='CJKBold')   # gen_pred_pdf 兼容 (无连字符粗体名)
"""
import os
import sys
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


def register_cjk_font(bold_name='CJK-Bold', prefer_lxgw=True):
    """注册 CJK 中文字体 (常规 + 粗体), 返回主字体名 'CJK'。

    回退链: 霞鹜文楷(LxgwWenKai) -> 脚本本地 fonts/ -> 解释器 fonts/ ->
    操作系统常见CJK字体 (Noto / WQY / Windows / macOS)。
    bold_name: 粗体被注册的字体内码名 (gen_pred_pdf 用 'CJKBold', 其余用 'CJK-Bold')。
    """
    # 1. 可选: 霞鹜文楷 (与至尊版预测PDF一致)
    if prefer_lxgw:
        lxgw = [
            ('/usr/share/fonts/truetype/LXGWWenKai-Regular.ttf', '/usr/share/fonts/truetype/LXGWWenKai-Medium.ttf'),
            ('/usr/share/fonts/truetype/lxgw/LXGWWenKai-Regular.ttf', '/usr/share/fonts/truetype/lxgw/LXGWWenKai-Medium.ttf'),
        ]
        for reg, bold in lxgw:
            if os.path.exists(reg) and os.path.exists(bold):
                try:
                    pdfmetrics.registerFont(TTFont('CJK', reg))
                    pdfmetrics.registerFont(TTFont(bold_name, bold))
                    return 'CJK'
                except Exception:
                    continue

    # 2. 脚本本地 fonts/ 目录 + 解释器目录
    candidates = []
    env_dir = os.environ.get('SPORTTERY_FONT_DIR')
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path(__file__).resolve().parent / 'fonts')
    candidates.append(Path(sys.executable).parent / 'fonts')
    candidates.append(Path(sys.executable).parent.parent / 'fonts')

    names = [
        ('NotoSansSC-Regular.ttf', 'NotoSansSC-Bold.ttf'),
        ('NotoSansCJKsc-Regular.ttf', 'NotoSansCJKsc-Bold.ttf'),
    ]
    for d in candidates:
        for reg, bold in names:
            if (d / reg).exists() and (d / bold).exists():
                pdfmetrics.registerFont(TTFont('CJK', str(d / reg)))
                pdfmetrics.registerFont(TTFont(bold_name, str(d / bold)))
                return 'CJK'

    # 3. 操作系统字体 (Linux Noto/WQY, Windows 黑体/雅黑, macOS 苹方)
    os_fonts = [
        ('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'),
        ('/usr/share/fonts/truetype/wqy/wqy-microhei.ttc', '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc'),
        ('/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc', '/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc'),
        ('/data/user/work/NotoSansCJKsc-Regular.ttf', '/data/user/work/NotoSansCJKsc-Bold.ttf'),
        ('C:/Windows/Fonts/simhei.ttf', 'C:/Windows/Fonts/simhei.ttf'),
        ('C:/Windows/Fonts/msyh.ttf', 'C:/Windows/Fonts/msyhbd.ttf'),
        ('/System/Library/Fonts/PingFang.ttc', '/System/Library/Fonts/PingFang.ttc'),
    ]
    for reg, bold in os_fonts:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont('CJK', reg, subfontIndex=0))
                pdfmetrics.registerFont(TTFont(bold_name, bold if os.path.exists(bold) else reg, subfontIndex=0))
                return 'CJK'
            except Exception:
                continue
    raise RuntimeError('未找到可用CJK字体, 请设置 SPORTTERY_FONT_DIR 或将字体放入 ./fonts/')