"""Byte-level format and validation tests, independent of a desktop session."""

from pathlib import Path
import struct
import tempfile
import unittest

from PIL import Image

from tools.xcursor import Frame, read_headers, write_xcursor


class XcursorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cursor"

    @staticmethod
    def frame(size=(2, 3), hotspot=(1, 2), delay=17, color=(255, 128, 65, 128)):
        return Frame(Image.new("RGBA", size, color), hotspot, delay)

    def test_header_toc_multiple_sizes_and_animation_order(self):
        write_xcursor(self.path, {
            48: [self.frame((4, 5), (3, 4), 0)],
            24: [self.frame(delay=17), self.frame(delay=83)],
        })
        data = self.path.read_bytes()
        self.assertEqual(data[:16], b"Xcur" + struct.pack("<3I", 16, 0x10000, 3))
        # Positions are absolute byte offsets; all fields have exactly 4 bytes.
        self.assertEqual(struct.unpack_from("<9I", data, 16), (
            0xFFFD0002, 24, 52,
            0xFFFD0002, 24, 112,
            0xFFFD0002, 48, 172,
        ))
        self.assertEqual(struct.unpack_from("<9I", data, 52),
                         (36, 0xFFFD0002, 24, 1, 2, 3, 1, 2, 17))
        self.assertEqual(len(data), 288)
        headers = read_headers(self.path)
        self.assertEqual([h["size"] for h in headers], [24, 24, 48])
        self.assertEqual([h["delay_ms"] for h in headers], [17, 83, 0])
        self.assertEqual((headers[2]["width"], headers[2]["height"]), (4, 5))
        self.assertEqual((headers[2]["xhot"], headers[2]["yhot"]), (3, 4))

    def test_premultiplied_pixels_and_little_endian_bgra(self):
        image = Image.new("RGBA", (4, 1))
        image.putdata([(255, 128, 65, 128), (9, 99, 199, 0),
                       (10, 20, 30, 255), (128, 255, 0, 1)])
        write_xcursor(self.path, {32: [Frame(image, (0, 0), 0)]})
        # Each uint32 is AARRGGBB; transparent colors must be cleared.
        self.assertEqual(self.path.read_bytes()[64:], bytes([
            33, 64, 128, 128, 0, 0, 0, 0,
            30, 20, 10, 255, 0, 1, 1, 1,
        ]))

    def test_premultiplication_matches_xcursorgen_for_all_byte_values(self):
        image = Image.new("RGBA", (256, 256))
        image.putdata([(channel, channel, channel, alpha)
                       for alpha in range(256) for channel in range(256)])
        write_xcursor(self.path, {256: [Frame(image, (0, 0), 0)]})
        data = self.path.read_bytes()[64:]
        for alpha in range(256):
            for channel in range(256):
                product = channel * alpha
                expected = (product + 128 + ((product + 128) >> 8)) >> 8
                offset = 4 * (alpha * 256 + channel)
                self.assertEqual(data[offset:offset + 4],
                                 bytes((expected, expected, expected, alpha)))

    def test_invalid_input_leaves_existing_file_untouched(self):
        invalid = [
            {}, {0: [self.frame()]}, {-1: [self.frame()]},
            {1 << 32: [self.frame()]}, {True: [self.frame()]}, {24: []},
            {24: [self.frame(size=(0, 3))]},
            {24: [self.frame(size=(32768, 1), hotspot=(0, 0))]},
            {24: [self.frame(hotspot=(-1, 0))]},
            {24: [self.frame(hotspot=(2, 0))]},
            {24: [self.frame(hotspot=(0, 3))]},
            {24: [self.frame(hotspot=(0.5, 0))]},
            {24: [self.frame(delay=-1)]}, {24: [self.frame(delay=1 << 32)]},
            {24: [Frame(Image.new("RGB", (2, 3)), (0, 0), 0)]},
        ]
        for sizes in invalid:
            with self.subTest(sizes=sizes):
                self.path.write_bytes(b"keep existing cursor")
                with self.assertRaises(ValueError):
                    write_xcursor(self.path, sizes)
                self.assertEqual(self.path.read_bytes(), b"keep existing cursor")

    def test_unsigned_delay_and_edge_hotspot(self):
        write_xcursor(self.path, {32: [self.frame(delay=0xFFFFFFFF)]})
        self.assertEqual(read_headers(self.path)[0]["delay_ms"], 0xFFFFFFFF)

    def test_rejects_malformed_headers_and_truncated_payloads(self):
        write_xcursor(self.path, {24: [self.frame()]})
        original = self.path.read_bytes()
        # word offsets: file magic, TOC count, TOC chunk offset, image subtype,
        # image width, and image xhot. The payload starts at byte 64.
        mutations = [(0, 0), (12, 999), (24, 0), (36, 99), (44, 0), (52, 2)]
        corrupted = [original[:12], original[:-1]]
        for offset, value in mutations:
            data = bytearray(original)
            struct.pack_into("<I", data, offset, value)
            corrupted.append(data)
        for data in corrupted:
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    read_headers(self.path)

    def test_rejects_duplicate_overlapping_toc_entries(self):
        write_xcursor(self.path, {24: [self.frame(), self.frame()]})
        data = bytearray(self.path.read_bytes())
        first_position = struct.unpack_from("<I", data, 24)[0]
        struct.pack_into("<I", data, 36, first_position)
        self.path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "overlapping"):
            read_headers(self.path)


if __name__ == "__main__":
    unittest.main()
