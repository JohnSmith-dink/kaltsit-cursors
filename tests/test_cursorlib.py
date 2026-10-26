"""Byte-level fixtures exercise transparency, resolution and ANI timelines."""

from io import BytesIO
from pathlib import Path
import struct
import tempfile
import unittest

from PIL import Image

from tools.cursorlib import CursorFormatError, load_cursor


def cur(entries):
    """entries: (width, height, hotspot, image payload)."""
    offset = 6 + 16 * len(entries)
    directory, payloads = [], []
    for width, height, hotspot, payload in entries:
        directory.append(struct.pack(
            "<4B2H2I", width % 256, height % 256, 0, 0,
            *hotspot, len(payload), offset,
        ))
        payloads.append(payload)
        offset += len(payload)
    return struct.pack("<3H", 0, 2, len(entries)) + b"".join(directory + payloads)


def png(width, height, color):
    output = BytesIO()
    Image.new("RGBA", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def dib(width, rows, depth, pixels, mask, palette=(), top_down=False):
    """Create a Windows DIB from top-to-bottom pixel and AND-mask rows."""
    xor_stride = ((width * depth + 31) // 32) * 4
    mask_stride = ((width + 31) // 32) * 4
    colors = b"".join(bytes((b, g, r, 0)) for r, g, b in palette)
    xor_rows, mask_rows = [], []
    for row, mask_row in zip(pixels, mask):
        if depth == 32:
            raw = b"".join(bytes((b, g, r, a)) for r, g, b, a in row)
        elif depth == 24:
            raw = b"".join(bytes((b, g, r)) for r, g, b in row)
        elif depth == 8:
            raw = bytes(row)
        elif depth == 1:
            raw = bytes(sum(bit << (7 - j) for j, bit in enumerate(row[i:i + 8]))
                        for i in range(0, width, 8))
        else:
            raise AssertionError("Fixture depth unsupported")
        xor_rows.append(raw.ljust(xor_stride, b"\0"))
        packed_mask = bytes(sum(bit << (7 - j) for j, bit in enumerate(mask_row[i:i + 8]))
                            for i in range(0, width, 8))
        mask_rows.append(packed_mask.ljust(mask_stride, b"\0"))
    if not top_down:
        xor_rows.reverse()
        mask_rows.reverse()
    header = struct.pack("<IiiHHIIiiII", 40, width, -2 * rows if top_down else 2 * rows,
                         1, depth, 0, xor_stride * rows, 0, 0, len(palette), 0)
    return header + colors + b"".join(xor_rows + mask_rows)


def chunk(kind, payload):
    return kind + struct.pack("<I", len(payload)) + payload + b"\0" * (len(payload) % 2)


def ani(images, *, rates=None, sequence=None, default_rate=6, steps=None):
    steps = steps if steps is not None else len(sequence) if sequence is not None else len(images)
    header = struct.pack("<9I", 36, len(images), steps, 0, 0, 0, 0,
                         default_rate, 1 | (2 if sequence is not None else 0))
    content = chunk(b"anih", header) + chunk(b"JUNK", b"x")
    if rates is not None:
        content += chunk(b"rate", struct.pack(f"<{len(rates)}I", *rates))
    if sequence is not None:
        content += chunk(b"seq ", struct.pack(f"<{len(sequence)}I", *sequence))
    content += chunk(b"LIST", b"fram" + b"".join(chunk(b"icon", image) for image in images))
    return b"RIFF" + struct.pack("<I", len(content) + 4) + b"ACON" + content


class CursorTests(unittest.TestCase):
    def load(self, data, size=2):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.cur"
            path.write_bytes(data)
            return load_cursor(path, size)

    def test_palette_and_mask_preserve_orientation_transparency_and_hotspot(self):
        payload = dib(2, 2, 8, [[1, 0], [0, 2]], [[0, 1], [1, 0]],
                      [(0, 0, 0), (255, 0, 0), (0, 0, 255)])
        frame, = self.load(cur([(2, 2, (1, 0), payload)]))
        self.assertEqual(frame.image.mode, "RGBA")
        self.assertEqual([frame.image.getpixel((x, y)) for y in range(2) for x in range(2)],
                         [(255, 0, 0, 255), (0, 0, 0, 0),
                          (0, 0, 0, 0), (0, 0, 255, 255)])
        self.assertEqual(frame.hotspot, (1, 0))
        self.assertEqual(frame.delay_ms, 0)

    def test_32bit_alpha_overrides_legacy_and_mask(self):
        payload = dib(2, 2, 32,
                      [[(12, 34, 56, 128), (0, 0, 0, 0)],
                       [(20, 40, 60, 255), (1, 2, 3, 64)]],
                      [[1, 1], [1, 1]])
        frame, = self.load(cur([(2, 2, (0, 1), payload)]))
        self.assertEqual([frame.image.getpixel((x, y)) for y in range(2) for x in range(2)],
                         [(12, 34, 56, 128), (0, 0, 0, 0),
                          (20, 40, 60, 255), (1, 2, 3, 64)])

    def test_zero_alpha_32bit_uses_and_mask(self):
        payload = dib(2, 2, 32,
                      [[(9, 8, 7, 0), (0, 0, 0, 0)],
                       [(0, 0, 0, 0), (4, 5, 6, 0)]],
                      [[0, 1], [1, 0]])
        frame, = self.load(cur([(2, 2, (0, 0), payload)]))
        self.assertEqual(frame.image.getpixel((0, 0)), (9, 8, 7, 255))
        self.assertEqual(frame.image.getpixel((1, 0))[3], 0)

    def test_top_down_and_24bit_row_padding(self):
        payload = dib(2, 2, 24, [[(255, 0, 0), (0, 0, 0)],
                                [(0, 255, 0), (0, 0, 0)]],
                      [[0, 1], [0, 1]], top_down=True)
        frame, = self.load(cur([(2, 2, (0, 0), payload)]))
        self.assertEqual(frame.image.getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(frame.image.getpixel((0, 1)), (0, 255, 0, 255))

    def test_monochrome_invert_is_reported_instead_of_erased(self):
        payload = dib(2, 2, 1, [[0, 1], [1, 0]], [[0, 0], [1, 1]],
                      [(0, 0, 0), (255, 255, 255)])
        with self.assertRaisesRegex(CursorFormatError, "XOR/invert"):
            self.load(cur([(2, 2, (0, 0), payload)]))

    def test_color_xor_is_also_reported(self):
        payload = dib(2, 2, 8, [[1, 0], [0, 0]], [[1, 1], [1, 1]],
                      [(0, 0, 0), (255, 0, 0)])
        with self.assertRaisesRegex(CursorFormatError, "XOR/invert"):
            self.load(cur([(2, 2, (0, 0), payload)]))

    def test_resolution_selection_and_scaled_hotspot(self):
        data = cur([(2, 2, (1, 1), png(2, 2, "red")),
                    (8, 8, (4, 6), png(8, 8, "blue")),
                    (4, 4, (2, 3), png(4, 4, "green"))])
        exact, = self.load(data, 4)
        self.assertEqual(exact.image.getpixel((0, 0)), (0, 128, 0, 255))
        larger, = self.load(data, 6)
        self.assertEqual(larger.image.getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(larger.hotspot, (3, 5))
        upsampled, = self.load(data, 16)
        self.assertEqual(upsampled.hotspot, (8, 12))

    def test_non_square_cursor_keeps_aspect_ratio(self):
        frame, = self.load(cur([(4, 2, (3, 1), png(4, 2, "red"))]), 8)
        self.assertEqual(frame.image.size, (8, 4))
        self.assertEqual(frame.hotspot, (6, 2))

    def test_zero_directory_dimensions_mean_256(self):
        frame, = self.load(cur([(256, 256, (128, 128), png(256, 256, "red"))]), 32)
        self.assertEqual(frame.image.size, (32, 32))
        self.assertEqual(frame.hotspot, (16, 16))

    def test_png_alpha_is_preserved(self):
        frame, = self.load(cur([(2, 2, (0, 0), png(2, 2, (10, 20, 30, 73)))]))
        self.assertEqual(frame.image.getpixel((0, 0)), (10, 20, 30, 73))

    def test_sequence_rates_and_per_frame_hotspots(self):
        red = cur([(2, 2, (0, 1), png(2, 2, "red"))])
        blue = cur([(2, 2, (1, 0), png(2, 2, "blue"))])
        frames = self.load(ani([red, blue], sequence=[1, 0, 1], rates=[1, 2, 3]))
        self.assertEqual([frame.delay_ms for frame in frames], [17, 33, 50])
        self.assertEqual([frame.hotspot for frame in frames], [(1, 0), (0, 1), (1, 0)])
        self.assertEqual([frame.image.getpixel((0, 0)) for frame in frames],
                         [(0, 0, 255, 255), (255, 0, 0, 255), (0, 0, 255, 255)])

    def test_default_rate_and_cumulative_jiffy_rounding(self):
        image = cur([(2, 2, (0, 0), png(2, 2, "red"))])
        self.assertEqual(self.load(ani([image]))[0].delay_ms, 100)
        frames = self.load(ani([image], sequence=[0] * 60, default_rate=1))
        self.assertEqual(sum(frame.delay_ms for frame in frames), 1000)
        self.assertEqual(set(frame.delay_ms for frame in frames), {16, 17})

    def test_invalid_ani_sequence_and_rate_table(self):
        image = cur([(2, 2, (0, 0), png(2, 2, "red"))])
        with self.assertRaisesRegex(CursorFormatError, "nonexistent frame"):
            self.load(ani([image], sequence=[1]))
        with self.assertRaisesRegex(CursorFormatError, "one entry per step"):
            self.load(ani([image], rates=[6, 6]))
        with self.assertRaisesRegex(CursorFormatError, "positive jiffies"):
            self.load(ani([image], default_rate=0))

    def test_truncation_and_out_of_bounds_hotspots_are_rejected(self):
        valid = cur([(2, 2, (0, 0), png(2, 2, "red"))])
        with self.assertRaisesRegex(CursorFormatError, "truncated CUR"):
            self.load(valid[:-1])
        with self.assertRaisesRegex(CursorFormatError, "hotspot"):
            self.load(cur([(2, 2, (2, 0), png(2, 2, "red"))]))
        with self.assertRaisesRegex(CursorFormatError, "RIFF size"):
            self.load(ani([valid])[:-1])


if __name__ == "__main__":
    unittest.main()
