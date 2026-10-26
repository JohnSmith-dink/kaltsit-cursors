"""Write animated, multi-size Xcursor files without an xcursorgen executable.

Format references (X.Org upstream):
https://www.x.org/releases/current/doc/man/man3/Xcursor.3.xhtml
https://www.x.org/releases/individual/lib/libXcursor-1.2.3.tar.xz
  include/X11/Xcursor/Xcursor.h.in and src/file.c
https://www.x.org/releases/individual/app/xcursorgen-1.0.9.tar.xz
  xcursorgen.c, premultiply_data()

Every integer is an unsigned 32-bit little-endian word. Pixels are premultiplied
ARGB words, which appear as B, G, R, A bytes on disk. Pillow's RGBa conversion
rounds channel * alpha / 255 to the nearest integer, as does xcursorgen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

from PIL import Image


MAGIC = 0x72756358
FILE_VERSION = 0x00010000
FILE_HEADER_SIZE = 16
TOC_SIZE = 12
IMAGE_TYPE = 0xFFFD0002
IMAGE_VERSION = 1
IMAGE_HEADER_SIZE = 36
MAX_DIMENSION = 0x7FFF
UINT32_MAX = 0xFFFFFFFF


@dataclass
class Frame:
    """One straight-alpha RGBA image, its pixel hotspot, and frame duration."""

    image: Image.Image
    hotspot: tuple[int, int]
    delay_ms: int


def _integer(value: int, label: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer in {minimum}..{maximum}")


def _validate_frame(frame: Frame) -> None:
    if not isinstance(frame.image, Image.Image) or frame.image.mode != "RGBA":
        raise ValueError("frame image must be a Pillow RGBA image")
    width, height = frame.image.size
    _integer(width, "image width", 1, MAX_DIMENSION)
    _integer(height, "image height", 1, MAX_DIMENSION)
    if not isinstance(frame.hotspot, (tuple, list)) or len(frame.hotspot) != 2:
        raise ValueError("hotspot must contain an x and y coordinate")
    _integer(frame.hotspot[0], "hotspot x", 0, width - 1)
    _integer(frame.hotspot[1], "hotspot y", 0, height - 1)
    _integer(frame.delay_ms, "frame delay", 0, UINT32_MAX)


def write_xcursor(path: Path, sizes: dict[int, list[Frame]]) -> None:
    """Write one cursor, preserving the input frame order within each size.

    ``sizes`` maps nominal sizes to nonempty frame sequences. Nominal sizes
    describe the requested UI scale and may differ from an image's dimensions.
    Size groups are sorted for deterministic output. Validation occurs before
    opening the destination, so invalid input never truncates an existing file.
    Parent directories must already exist. Raises ValueError for invalid data.
    """
    if not sizes:
        raise ValueError("at least one size is required")
    for size, frames in sizes.items():
        _integer(size, "nominal size", 1, UINT32_MAX)
        if not frames:
            raise ValueError(f"size {size} must contain at least one frame")
        for frame in frames:
            _validate_frame(frame)

    entries = [(size, frame) for size in sorted(sizes) for frame in sizes[size]]
    _integer(len(entries), "frame count", 1, UINT32_MAX)
    position = FILE_HEADER_SIZE + len(entries) * TOC_SIZE
    toc = bytearray()
    for size, frame in entries:
        _integer(position, "chunk offset", 0, UINT32_MAX)
        toc.extend(struct.pack("<3I", IMAGE_TYPE, size, position))
        position += IMAGE_HEADER_SIZE + frame.image.width * frame.image.height * 4
    _integer(position, "file length", 0, UINT32_MAX)

    with Path(path).open("wb") as stream:
        stream.write(struct.pack("<4I", MAGIC, FILE_HEADER_SIZE, FILE_VERSION, len(entries)))
        stream.write(toc)
        for size, frame in entries:
            stream.write(struct.pack(
                "<9I", IMAGE_HEADER_SIZE, IMAGE_TYPE, size, IMAGE_VERSION,
                frame.image.width, frame.image.height,
                frame.hotspot[0], frame.hotspot[1], frame.delay_ms,
            ))
            stream.write(frame.image.convert("RGBa").tobytes("raw", "BGRa"))


def read_headers(path: Path) -> list[dict[str, int]]:
    """Validate an image-only Xcursor and return frame metadata in TOC order.

    This inspection helper checks headers, TOC consistency, pixel payload
    bounds, dimensions, hotspots, and overlapping chunks. It does not decode
    pixels or support comment/unknown chunk types. Extended header lengths are
    accepted, allowing compatible additions to the current format.
    """
    data = Path(path).read_bytes()
    if len(data) < FILE_HEADER_SIZE:
        raise ValueError("truncated Xcursor file header")
    magic, header_size, version, count = struct.unpack_from("<4I", data)
    if magic != MAGIC:
        raise ValueError("invalid Xcursor magic")
    if version >> 16 != FILE_VERSION >> 16:
        raise ValueError("unsupported Xcursor major version")
    if header_size < FILE_HEADER_SIZE or header_size > len(data):
        raise ValueError("invalid Xcursor file header size")
    toc_end = header_size + count * TOC_SIZE
    if not count or toc_end > len(data):
        raise ValueError("empty or truncated Xcursor table of contents")

    result = []
    intervals = []
    for index in range(count):
        kind, size, position = struct.unpack_from("<3I", data, header_size + index * TOC_SIZE)
        if kind != IMAGE_TYPE:
            raise ValueError("read_headers only supports image chunks")
        _integer(size, "nominal size", 1, UINT32_MAX)
        if position < toc_end or position + IMAGE_HEADER_SIZE > len(data):
            raise ValueError("image chunk lies outside the payload area")
        (chunk_header, chunk_type, chunk_size, chunk_version,
         width, height, xhot, yhot, delay) = struct.unpack_from("<9I", data, position)
        if chunk_type != kind or chunk_size != size:
            raise ValueError("image chunk does not match its TOC entry")
        if chunk_version != IMAGE_VERSION:
            raise ValueError("unsupported image chunk version")
        if chunk_header < IMAGE_HEADER_SIZE:
            raise ValueError("image chunk header is too short")
        _integer(width, "image width", 1, MAX_DIMENSION)
        _integer(height, "image height", 1, MAX_DIMENSION)
        _integer(xhot, "hotspot x", 0, width - 1)
        _integer(yhot, "hotspot y", 0, height - 1)
        pixels_offset = position + chunk_header
        end = pixels_offset + width * height * 4
        if end > len(data):
            raise ValueError("truncated image pixel payload")
        intervals.append((position, end))
        result.append({
            "size": size, "width": width, "height": height,
            "xhot": xhot, "yhot": yhot, "delay_ms": delay,
            "position": position, "pixels_offset": pixels_offset,
        })

    previous_end = toc_end
    for start, end in sorted(intervals):
        if start < previous_end:
            raise ValueError("overlapping image chunks")
        previous_end = end
    return result
