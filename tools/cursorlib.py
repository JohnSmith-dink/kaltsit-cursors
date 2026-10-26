"""Decode Windows CUR and RIFF/ACON ANI files without losing cursor metadata.

Pillow decodes the pixel formats; this module handles directories, hotspots,
AND masks and animation timelines explicitly. Screen-dependent XOR pixels
cannot be expressed as RGBA and raise CursorFormatError instead of disappearing.

Format references:
https://learn.microsoft.com/en-us/windows/win32/menurc/using-cursors
https://devblogs.microsoft.com/oldnewthing/20101021-00/?p=12483
https://pillow.readthedocs.io/en/stable/reference/plugins.html
Microsoft, Multimedia Data Standards Update (1994), "ACON" (pp. 9–10):
https://billposer.org/Linguistics/Computation/riffnew.pdf
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import struct
from typing import Iterator

from PIL import Image, ImageChops, ImageOps


class CursorFormatError(ValueError):
    """The input is malformed or cannot be faithfully represented as RGBA."""


@dataclass
class Frame:
    image: Image.Image
    hotspot: tuple[int, int]
    delay_ms: int


@dataclass(frozen=True)
class _Entry:
    width: int
    height: int
    hotspot: tuple[int, int]
    depth: int
    payload: bytes


def _decode_dib(payload: bytes, dimensions: tuple[int, int]) -> Image.Image:
    if len(payload) < 12:
        raise CursorFormatError("Truncated cursor DIB header")
    header_size = struct.unpack_from("<I", payload)[0]
    if header_size == 12:
        width, height, planes, depth = struct.unpack_from("<4H", payload, 4)
        compression, colors_used, palette_stride = 0, 0, 3
    elif header_size in (40, 52, 56, 108, 124):
        if len(payload) < header_size:
            raise CursorFormatError("Truncated cursor DIB header")
        width, height, planes, depth, compression = struct.unpack_from(
            "<iiHHI", payload, 4
        )
        colors_used = struct.unpack_from("<I", payload, 32)[0]
        palette_stride = 4
    else:
        raise CursorFormatError(f"Unsupported DIB header size: {header_size}")
    if width != dimensions[0] or abs(height) != dimensions[1] * 2:
        raise CursorFormatError("DIB dimensions disagree with the CUR directory")
    if planes != 1 or depth not in (1, 4, 8, 16, 24, 32):
        raise CursorFormatError(f"Unsupported DIB planes/bit depth: {planes}/{depth}")
    if compression not in (0, 3) or (compression == 3 and depth not in (16, 32)):
        raise CursorFormatError(f"Unsupported cursor DIB compression: {compression}")

    rows = dimensions[1]
    orientation = -1 if height > 0 else 1
    palette_count = colors_used or (1 << depth if depth <= 8 else 0)
    if depth <= 8 and palette_count > (1 << depth):
        raise CursorFormatError("DIB palette has too many colors")
    external_masks = 12 if compression == 3 and header_size == 40 else 0
    pixel_offset = header_size + external_masks + palette_count * palette_stride
    xor_stride = ((width * depth + 31) // 32) * 4
    mask_stride = ((width + 31) // 32) * 4
    xor_end = pixel_offset + xor_stride * rows
    mask_end = xor_end + mask_stride * rows
    if mask_end > len(payload):
        raise CursorFormatError("Truncated cursor XOR bitmap or AND mask")

    # Build an ordinary BMP containing only the XOR/color image. Do not ask
    # Pillow's CUR plugin to pick the resolution or infer transparency.
    dib = bytearray(payload[:xor_end])
    if header_size == 12:
        struct.pack_into("<H", dib, 6, rows)
    else:
        struct.pack_into("<i", dib, 8, rows if height > 0 else -rows)
        struct.pack_into("<I", dib, 20, xor_stride * rows)
    bmp = struct.pack("<2sIHHI", b"BM", 14 + len(dib), 0, 0, 14 + pixel_offset)
    try:
        with Image.open(BytesIO(bmp + dib)) as decoded:
            image = decoded.convert("RGBA")
    except (OSError, ValueError) as exc:
        raise CursorFormatError(f"Unsupported cursor bitmap: {exc}") from exc

    # BI_RGB's fourth byte is alpha only when at least one pixel uses it.
    # All-zero alpha is the older 0RGB format and must fall back to the mask.
    alpha = None
    if depth == 32 and compression == 0:
        color = Image.frombytes(
            "RGBA", dimensions, payload[pixel_offset:xor_end], "raw",
            "BGRA", xor_stride, orientation,
        )
        alpha = color.getchannel("A")
        image = color
    elif depth == 32 and compression == 3 and header_size >= 56:
        alpha_mask = struct.unpack_from("<I", payload, 52)[0]
        if alpha_mask:
            alpha = image.getchannel("A")
    if alpha is not None and alpha.getextrema()[1] > 0:
        # Windows uses alpha blending here; the legacy AND mask is ignored.
        return image

    and_mask = Image.frombytes(
        "1", dimensions, payload[xor_end:mask_end], "raw",
        "1", mask_stride, orientation,
    ).convert("L")
    red, green, blue = image.convert("RGB").split()
    nonzero_color = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    if ImageChops.multiply(nonzero_color, and_mask).getbbox() is not None:
        raise CursorFormatError(
            "Cursor contains screen-dependent XOR/invert pixels (AND=1 and "
            "nonzero XOR color); RGBA/Xcursor cannot preserve that effect"
        )
    image.putalpha(ImageOps.invert(and_mask))
    return image


def _load_cur(data: bytes, size: int) -> Frame:
    if len(data) < 6:
        raise CursorFormatError("Truncated CUR header")
    reserved, kind, count = struct.unpack_from("<3H", data)
    if reserved != 0 or kind != 2 or count == 0:
        raise CursorFormatError("Expected a CUR file with at least one image")
    directory_end = 6 + count * 16
    if directory_end > len(data):
        raise CursorFormatError("Truncated CUR directory")
    entries = []
    for index in range(count):
        w, h, _, _, x, y, length, offset = struct.unpack_from(
            "<4B2H2I", data, 6 + index * 16
        )
        width, height = w or 256, h or 256
        if x >= width or y >= height:
            raise CursorFormatError("Cursor hotspot lies outside its image")
        if offset < directory_end or length < 12 or offset + length > len(data):
            raise CursorFormatError("Invalid or truncated CUR image payload")
        payload = data[offset:offset + length]
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            depth = 32
        else:
            header_size = struct.unpack_from("<I", payload)[0]
            depth_offset = 10 if header_size == 12 else 14
            if len(payload) < depth_offset + 2:
                raise CursorFormatError("Truncated DIB bit depth")
            depth = struct.unpack_from("<H", payload, depth_offset)[0]
        entries.append(_Entry(width, height, (x, y), depth, payload))

    # Prefer an exact match, then the smallest image large enough to avoid
    # upsampling. If every source is smaller, use the largest available one.
    def preference(entry: _Entry) -> tuple[int, int, int]:
        extent = max(entry.width, entry.height)
        return (0, extent - size, -entry.depth) if extent >= size else (
            1, size - extent, -entry.depth
        )

    entry = min(entries, key=preference)
    dimensions = entry.width, entry.height
    if entry.payload.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            with Image.open(BytesIO(entry.payload)) as decoded:
                if decoded.format != "PNG" or decoded.size != dimensions:
                    raise CursorFormatError("PNG dimensions disagree with CUR directory")
                image = decoded.convert("RGBA")
        except (OSError, ValueError) as exc:
            raise CursorFormatError(f"Invalid PNG cursor: {exc}") from exc
    else:
        image = _decode_dib(entry.payload, dimensions)

    extent = max(dimensions)
    target = tuple(max(1, (side * size + extent // 2) // extent) for side in dimensions)
    hotspot = tuple(
        min(new - 1, (point * new + old // 2) // old)
        for point, old, new in zip(entry.hotspot, dimensions, target)
    )
    if target != dimensions:
        # The source artwork is pixel art: preserve its hard edges and palette.
        image = image.resize(target, Image.Resampling.NEAREST)
    return Frame(image, hotspot, 0)


def _chunks(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, bytes]]:
    position = start
    while position < end:
        if position + 8 > end:
            raise CursorFormatError("Truncated RIFF chunk header")
        kind, length = struct.unpack_from("<4sI", data, position)
        payload_start = position + 8
        next_position = payload_start + length + (length & 1)
        if next_position > end:
            raise CursorFormatError("Truncated RIFF chunk or padding")
        yield kind, data[payload_start:payload_start + length]
        position = next_position


def _load_ani(data: bytes, size: int) -> list[Frame]:
    if len(data) < 12 or data[8:12] != b"ACON":
        raise CursorFormatError("Expected a RIFF/ACON animated cursor")
    end = struct.unpack_from("<I", data, 4)[0] + 8
    if end != len(data):
        raise CursorFormatError("RIFF size does not match the file length")
    tables: dict[bytes, bytes] = {}
    icons = []
    for kind, payload in _chunks(data, 12, end):
        if kind in (b"anih", b"rate", b"seq "):
            if kind in tables:
                raise CursorFormatError(f"Duplicate ANI {kind!r} chunk")
            tables[kind] = payload
        elif kind == b"LIST":
            if len(payload) < 4:
                raise CursorFormatError("Truncated RIFF LIST type")
            if payload[:4] == b"fram":
                icons.extend(p for k, p in _chunks(payload, 4, len(payload)) if k == b"icon")
    header = tables.get(b"anih", b"")
    if len(header) < 36:
        raise CursorFormatError("Missing or truncated ANI header")
    header_size, count, steps, _, _, _, _, default_rate, flags = struct.unpack_from("<9I", header)
    if header_size < 36 or header_size > len(header):
        raise CursorFormatError("Invalid ANI header size")
    if not flags & 1:
        raise CursorFormatError("Raw-bitmap ANI frames are unsupported; expected AF_ICON")
    if count != len(icons) or count == 0 or steps == 0:
        raise CursorFormatError("ANI frame/step counts disagree with its contents")

    def table(kind: bytes) -> tuple[int, ...] | None:
        payload = tables.get(kind)
        if payload is None:
            return None
        if len(payload) != steps * 4:
            raise CursorFormatError(f"ANI {kind!r} table must have one entry per step")
        return struct.unpack(f"<{steps}I", payload)

    sequence = table(b"seq ")
    if flags & 2 and sequence is None:
        raise CursorFormatError("ANI declares AF_SEQUENCE but has no seq table")
    if sequence is None:
        if steps != count:
            raise CursorFormatError("ANI without a seq table must have one step per frame")
        sequence = tuple(range(count))
    if any(index >= count for index in sequence):
        raise CursorFormatError("ANI sequence references a nonexistent frame")
    rates = table(b"rate") or (default_rate,) * steps
    if any(rate == 0 for rate in rates):
        raise CursorFormatError("ANI frame delays must be positive jiffies")

    decoded = [_load_cur(icon, size) for icon in icons]
    frames = []
    elapsed_jiffies = elapsed_ms = 0
    for index, rate in zip(sequence, rates):
        # Round cumulative time so 60 one-jiffy steps last 1000 ms, not 1020.
        elapsed_jiffies += rate
        next_ms = (elapsed_jiffies * 1000 + 30) // 60
        source = decoded[index]
        frames.append(Frame(source.image.copy(), source.hotspot, next_ms - elapsed_ms))
        elapsed_ms = next_ms
    return frames


def load_cursor(path: Path, size: int) -> list[Frame]:
    """Load CUR/ANI as RGBA frames, keeping hotspots and playback order.

    The longest image dimension becomes ``size``; aspect ratio is preserved.
    Static frames have a zero delay. ANI's 1/60-second jiffies are rounded to
    integer milliseconds with cumulative rounding to avoid timeline drift.
    Unsupported screen-dependent XOR pixels raise ``CursorFormatError``.
    """
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError("Cursor size must be a positive integer")
    path = Path(path)
    data = path.read_bytes()
    try:
        if data[:4] == b"RIFF":
            return _load_ani(data, size)
        return [_load_cur(data, size)]
    except CursorFormatError as exc:
        raise CursorFormatError(f"{path.name}: {exc}") from exc
