"""Minimal ISO-BMFF inspection for stored clips.

Clips are only ever stored after this module has read them back: the file
must start with ``ftyp``, carry a ``moov`` with a video track whose sample
entry is H.264/H.265, and its *actual* duration is computed from the media
itself (``mdhd`` timescale plus fragment sample durations, or the
non-fragmented ``mdhd`` duration). The duration shown to a user is
therefore what the bytes contain, never the duration that was requested.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

VIDEO_CODECS = frozenset({"avc1", "avc3", "hvc1", "hev1"})
_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"minf", b"stbl", b"moof", b"traf", b"mvex", b"edts"})


class InvalidClipError(ValueError):
    """The bytes are not a playable MP4 video clip."""


@dataclass(frozen=True)
class Mp4Info:
    codec: str
    duration_seconds: float
    fragmented: bool
    width: int | None = None
    height: int | None = None


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    end = len(data) if end is None else end
    offset = start
    while offset + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            return
        yield kind, offset + header, offset + size
        offset += size


def _child(data: bytes, start: int, end: int, kind: bytes):
    for name, body, stop in _boxes(data, start, end):
        if name == kind:
            return body, stop
    return None


def _track(data: bytes, body: int, end: int) -> dict:
    info: dict = {"id": None, "handler": None, "timescale": None, "duration": 0, "codec": None,
                  "width": None, "height": None}
    tkhd = _child(data, body, end, b"tkhd")
    if tkhd:
        version = data[tkhd[0]]
        info["id"] = struct.unpack_from(">I", data, tkhd[0] + (20 if version == 1 else 12))[0]
        if tkhd[1] - 8 >= tkhd[0]:
            width, height = struct.unpack_from(">II", data, tkhd[1] - 8)
            info["width"], info["height"] = width >> 16, height >> 16
    mdia = _child(data, body, end, b"mdia")
    if not mdia:
        return info
    mdhd = _child(data, mdia[0], mdia[1], b"mdhd")
    if mdhd:
        version = data[mdhd[0]]
        if version == 1:
            info["timescale"], info["duration"] = struct.unpack_from(">IQ", data, mdhd[0] + 20)
        else:
            info["timescale"], info["duration"] = struct.unpack_from(">II", data, mdhd[0] + 12)
    hdlr = _child(data, mdia[0], mdia[1], b"hdlr")
    if hdlr:
        info["handler"] = data[hdlr[0] + 8:hdlr[0] + 12].decode("latin-1")
    minf = _child(data, mdia[0], mdia[1], b"minf")
    stbl = _child(data, minf[0], minf[1], b"stbl") if minf else None
    stsd = _child(data, stbl[0], stbl[1], b"stsd") if stbl else None
    if stsd and stsd[0] + 16 <= stsd[1]:
        info["codec"] = data[stsd[0] + 12:stsd[0] + 16].decode("latin-1")
    return info


def inspect(data: bytes) -> Mp4Info:
    """Validate ``data`` as an H.264/H.265 MP4 and measure its duration."""
    try:
        return _inspect(bytes(data))
    except (struct.error, IndexError, TypeError, ZeroDivisionError) as exc:
        raise InvalidClipError("truncated or malformed MP4") from exc


def _inspect(data: bytes) -> Mp4Info:
    top = list(_boxes(data))
    if not top or top[0][0] != b"ftyp":
        raise InvalidClipError("not an MP4 file (no leading ftyp box)")
    moov = next(((body, end) for kind, body, end in top if kind == b"moov"), None)
    if moov is None:
        raise InvalidClipError("MP4 has no moov box")
    tracks = [_track(data, body, end) for kind, body, end in _boxes(data, *moov) if kind == b"trak"]
    video = next((track for track in tracks if track["handler"] == "vide"), None)
    if video is None or not video["timescale"]:
        raise InvalidClipError("MP4 has no video track")
    if video["codec"] not in VIDEO_CODECS:
        raise InvalidClipError(f"unsupported video codec {video['codec']!r}")
    trex_default = 0
    mvex = _child(data, moov[0], moov[1], b"mvex")
    if mvex:
        for kind, body, _end in _boxes(data, *mvex):
            if kind == b"trex" and struct.unpack_from(">I", data, body + 4)[0] == video["id"]:
                trex_default = struct.unpack_from(">I", data, body + 12)[0]
    ticks = 0
    fragments = 0
    for kind, body, end in top:
        if kind != b"moof":
            continue
        for traf_kind, traf_body, traf_end in _boxes(data, body, end):
            if traf_kind != b"traf":
                continue
            tfhd = _child(data, traf_body, traf_end, b"tfhd")
            if not tfhd:
                continue
            flags = struct.unpack_from(">I", data, tfhd[0])[0] & 0xFFFFFF
            track_id = struct.unpack_from(">I", data, tfhd[0] + 4)[0]
            if track_id != video["id"]:
                continue
            fragments += 1
            cursor = tfhd[0] + 8
            if flags & 0x01:
                cursor += 8
            if flags & 0x02:
                cursor += 4
            default_duration = trex_default
            if flags & 0x08:
                default_duration = struct.unpack_from(">I", data, cursor)[0]
            for run_kind, run_body, _run_end in _boxes(data, traf_body, traf_end):
                if run_kind != b"trun":
                    continue
                run_flags = struct.unpack_from(">I", data, run_body)[0] & 0xFFFFFF
                count = struct.unpack_from(">I", data, run_body + 4)[0]
                if count > len(data):
                    raise InvalidClipError("implausible sample count")
                cursor = run_body + 8
                if run_flags & 0x01:
                    cursor += 4
                if run_flags & 0x04:
                    cursor += 4
                per_sample = sum(4 for bit in (0x100, 0x200, 0x400, 0x800) if run_flags & bit)
                if run_flags & 0x100:
                    for index in range(count):
                        ticks += struct.unpack_from(">I", data, cursor + index * per_sample)[0]
                else:
                    ticks += default_duration * count
    if fragments:
        duration = ticks / video["timescale"]
    else:
        duration = video["duration"] / video["timescale"]
    if duration <= 0:
        raise InvalidClipError("MP4 contains no video samples")
    return Mp4Info(
        codec=video["codec"], duration_seconds=round(duration, 3), fragmented=bool(fragments),
        width=video["width"] or None, height=video["height"] or None,
    )
