"""本地标签照片：不依赖第三方图像库，用标准库 zlib+struct 直接编码 PNG。

照片为模拟的布质标签照片：圆角边框、大号睡袋编号、由编号派生的
扫描码图案。仅用于本地演示与测试，真实部署可替换为相机照片。
"""
from __future__ import annotations

import hashlib
import os
import struct
import zlib

# 5x7 点阵字体（行序，每行 5 像素）
_FONT = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "#": ["01010", "01010", "11111", "01010", "11111", "01010", "01010"],
    " ": ["00000"] * 7,
}

# 与页面一致的温和配色（RGB）
BG = (251, 247, 240)
INK = (31, 58, 61)
ACCENT = (46, 111, 98)
PAPER = (255, 252, 246)
LINE = (196, 184, 166)


class Canvas:
    def __init__(self, w: int, h: int, bg=BG):
        self.w, self.h = w, h
        self.px = bytearray(bg * w * h)

    def set(self, x, y, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(color)

    def rect(self, x, y, w, h, color):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set(xx, yy, color)

    def outline(self, x, y, w, h, color, t=2):
        self.rect(x, y, w, t, color)
        self.rect(x, y + h - t, w, t, color)
        self.rect(x, y, t, h, color)
        self.rect(x + w - t, y, t, h, color)

    def text(self, s: str, x: int, y: int, scale: int, color=INK):
        cx = x
        for ch in s.upper():
            glyph = _FONT.get(ch, _FONT[" "])
            for r, row in enumerate(glyph):
                for c, bit in enumerate(row):
                    if bit == "1":
                        self.rect(cx + c * scale, y + r * scale, scale, scale, color)
            cx += 6 * scale

    def text_width(self, s: str, scale: int) -> int:
        return len(s) * 6 * scale - scale

    def png(self) -> bytes:
        raw = bytearray()
        row_bytes = self.w * 3
        for y in range(self.h):
            raw.append(0)  # filter type 0
            raw += self.px[y * row_bytes:(y + 1) * row_bytes]
        comp = zlib.compress(bytes(raw), 9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2,
                                             0, 0, 0))
                + chunk(b"IDAT", comp)
                + chunk(b"IEND", b""))


def _qr_like(seed_text: str, n: int = 21, cell: int = 6) -> tuple:
    """由编号确定性派生的扫描码图案（非真 QR，仅本地占位）。"""
    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    grid = [[0] * n for _ in range(n)]
    bit = 0
    for r in range(n):
        for c in range(n):
            grid[r][c] = (digest[(bit // 8) % len(digest)] >> (bit % 8)) & 1
            bit += 1
    # 三个定位角
    def finder(gr, gc):
        for r in range(7):
            for c in range(7):
                ring = r in (0, 6) or c in (0, 6)
                core = 2 <= r <= 4 and 2 <= c <= 4
                grid[gr + r][gc + c] = 1 if (ring or core) else 0
    finder(0, 0)
    finder(0, n - 7)
    finder(n - 7, 0)
    return grid, cell


def render_label_png(bag_no: str, size: tuple = (640, 400)) -> bytes:
    w, h = size
    cv = Canvas(w, h, BG)
    cv.outline(18, 18, w - 36, h - 36, ACCENT, 4)
    cv.outline(30, 30, w - 60, h - 60, LINE, 2)
    cv.text("NAP BAG LABEL", 52, 50, 3, ACCENT)

    scale = 9
    tw = cv.text_width(bag_no, scale)
    cv.text(bag_no, (w - tw) // 2, 150, scale, INK)

    cv.text("KINDERGARTEN  /  SCAN ME", 52, h - 70, 2, LINE)

    grid, cell = _qr_like(bag_no)
    qsize = len(grid) * cell
    qx, qy = w - 52 - qsize, h - 52 - qsize
    cv.rect(qx - 10, qy - 10, qsize + 20, qsize + 20, PAPER)
    for r, row in enumerate(grid):
        for c, bit in enumerate(row):
            if bit:
                cv.rect(qx + c * cell, qy + r * cell, cell, cell, INK)
    return cv.png()


def save_label_photo(bag_no: str, directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    safe = "".join(ch for ch in bag_no if ch.isalnum() or ch in "-_")
    path = os.path.join(directory, f"label-{safe}.png")
    with open(path, "wb") as f:
        f.write(render_label_png(bag_no))
    return path
