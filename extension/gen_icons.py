"""
Generate minimal PNG icons for the extension without requiring Pillow.
Run once: python gen_icons.py
"""
import struct, zlib, os

def make_png(size: int, bg=(26, 26, 46), fg=(0, 212, 170)) -> bytes:
    """Create a solid-colour PNG with a centred cross-hair mark."""
    w = h = size
    raw = []
    cx, cy, r = w // 2, h // 2, max(size // 4, 2)
    for y in range(h):
        row = [0]  # filter byte
        for x in range(w):
            in_h = (cy - 1 <= y <= cy + 1) and (cx - r <= x <= cx + r)
            in_v = (cx - 1 <= x <= cx + 1) and (cy - r <= y <= cy + r)
            c = fg if (in_h or in_v) else bg
            row += list(c)
        raw.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    compressed = zlib.compress(b"".join(raw), 9)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "icons")
    os.makedirs(out, exist_ok=True)
    for sz in (16, 48, 128):
        path = os.path.join(out, f"icon{sz}.png")
        with open(path, "wb") as f:
            f.write(make_png(sz))
        print(f"Generated {path}")
