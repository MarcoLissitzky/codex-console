"""把控制台用的字体子集化成 woff2，放进 fonts/。字体文件不随仓库分发（各自的 OFL 许可要求另附许可文本，
而且部分字体保留了字体名，子集化后的文件不宜再用原名分发），需要的话自己生成。

用法：pip install fonttools brotli
      python tools/build_fonts.py <放源字体的目录>

源字体（都在 Google Fonts / Noto 官方仓库，SIL OFL 1.1）：
  Libre Caslon Display / Libre Caslon Text、IBM Plex Mono、Noto Serif CJK SC、
  Barlow / Barlow Condensed / Barlow Semi Condensed
没有这些字体时，页面会退回系统字体（Windows 上是微软雅黑等），功能不受影响。
"""
import os
import sys

from fontTools import subset

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.fonts")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fonts")
os.makedirs(OUT, exist_ok=True)

LATIN = ("U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+2000-206F,"
         "U+2070-209F,U+20AC,U+2122,U+2190-21FF,U+2212,U+2215,U+25A0-25FF,U+00B7")

# 中文只取 GB2312 常用字 + 标点，Noto Serif SC 从十几 MB 降到 2 MB 左右
gb = set()
for a in range(0xA1, 0xF8):
    for b in range(0xA1, 0xFF):
        try:
            gb.add(ord(bytes([a, b]).decode("gb2312")))
        except UnicodeDecodeError:
            pass
gb |= set(range(0x20, 0x7F)) | set(range(0x3000, 0x3040)) | set(range(0xFF00, 0xFFF0))
gb |= set(range(0x2000, 0x2070)) | set(range(0x2190, 0x2200)) | {0xA7, 0xB7, 0xD7}
CJK = ",".join("U+%04X" % c for c in sorted(gb))

JOBS = {
    "LibreCaslonDisplay-Regular": ("LibreCaslonDisplay-Regular.ttf", LATIN),
    "LibreCaslonText-Regular": ("LibreCaslonText-Regular.ttf", LATIN),
    "LibreCaslonText-Italic": ("LibreCaslonText-Italic.ttf", LATIN),
    "LibreCaslonText-Bold": ("LibreCaslonText-Bold.ttf", LATIN),
    "IBMPlexMono-Regular": ("IBMPlexMono-Regular.ttf", LATIN),
    "NotoSerifSC-Regular": ("NotoSerifCJKsc-Regular.otf", CJK),
    "NotoSerifSC-Bold": ("NotoSerifCJKsc-Bold.otf", CJK),
    "Barlow-Regular": ("Barlow-Regular.ttf", LATIN),
    "Barlow-Italic": ("Barlow-Italic.ttf", LATIN),
    "Barlow-SemiBold": ("Barlow-SemiBold.ttf", LATIN),
    "BarlowCondensed-Bold": ("BarlowCondensed-Bold.ttf", LATIN),
    "BarlowCondensed-ExtraBold": ("BarlowCondensed-ExtraBold.ttf", LATIN),
    "BarlowSemiCondensed-SemiBold": ("BarlowSemiCondensed-SemiBold.ttf", LATIN),
    "BarlowSemiCondensed-Bold": ("BarlowSemiCondensed-Bold.ttf", LATIN),
}

for name, (src, uni) in JOBS.items():
    path = os.path.join(SRC, src)
    if not os.path.exists(path):
        print(f"缺 {src}，跳过")
        continue
    out = os.path.join(OUT, name + ".woff2")
    subset.main([path, "--unicodes=" + uni, "--flavor=woff2", "--layout-features=*",
                 "--output-file=" + out, "--no-hinting", "--desubroutinize"])
    print(f"{name:32s} {os.path.getsize(out) // 1024:6d} KB")
