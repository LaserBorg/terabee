#!/usr/bin/env python3
"""Terabee 3Dcam 8060 - raw depth capture, viewer and lens-mode switch.

The 3Dcam 8060 is a rebadged LIPSedge M3 (LIPS Corp, TI OPT8320 sensor). It
enumerates as a UVC camera and reports 160x60 YUYV, but the 19200-byte payload
is not colour video: it is 80x60 pixels of 4 bytes, two little-endian uint16
words per pixel.

    w0 = bytes[0:2]   0..13 in practice - NOT a second depth channel
    w1 = bytes[2:4]   bit 0-11 depth phase, bit 12-15 rolling frame counter

Two things about w1 matter more than anything else here:

1. **The top 4 bits are a frame counter, not data.** One value for the whole
   frame, cycling 0,1,2,3. Left in, the same pixel reads
   ``5774, 9748, 13595, 1648, ...`` as the counter steps; masked it reads
   ``1678, 1556, 1307, 1648, ...``. 75% of pixels exceed 4095 unmasked, and
   because higher raw counts mean *nearer*, the overflow makes near surfaces
   flicker black. Mask it with :data:`DEPTH_MASK`.

2. **Higher raw counts mean nearer.** Inverted versus most depth sensors, and
   versus what "distance" implies. :data:`NEAR_IS_HIGH` names this so the
   renderer does not encode it invisibly.

Values at/above :data:`OUT_OF_RANGE` are full-scale returns meaning "no
reading", not "very far away": exclude them, never smooth over them.

Range modes (``lens_mode``): the camera has a close (0.2-1.2 m) and a normal
(1-4 m) working range. Switching is done by the vendor's OpenNI2 driver, which
re-selects calibration data and re-programs the depth engine - it is not a V4L2
control. See :mod:`lens_mode`; it is optional, and the viewer runs without it.

Usage
-----
    python3 terabee3dcam.py                  # live viewer, q quits
    python3 terabee3dcam.py --seconds 5      # run for 5 s then exit
    python3 terabee3dcam.py --channels       # raw channel inspector
    python3 terabee3dcam.py --stats          # one-line statistics
    python3 terabee3dcam.py --mode close     # switch range mode, then view
    python3 terabee3dcam.py --list-modes     # current mode and config paths
"""

from __future__ import annotations

import argparse
import fcntl
import mmap
import os
import select
import struct
import sys
import time

import cv2
import numpy as np

# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------
WIDTH, HEIGHT = 80, 60
FRAME_BYTES = WIDTH * HEIGHT * 4           # 19200

#: Depth phase occupies bits 0-11.
DEPTH_BITS = 12
DEPTH_MASK = (1 << DEPTH_BITS) - 1

#: Bits 12-15 are the rolling frame counter (see module docstring).
COUNTER_SHIFT = DEPTH_BITS
FRAME_COUNTER_STEP = 1 << DEPTH_BITS       # how much one counter step adds

#: Full-scale returns mean "no reading". Exclude, do not smooth.
OUT_OF_RANGE = DEPTH_MASK - 1              # 4094

#: Larger raw counts mean nearer surfaces.
NEAR_IS_HIGH = True

#: The sensor is mounted upside down in use.
ROTATION = 180

#: Default display window, in raw counts: the whole 12-bit phase. Chosen
#: because it is the only *non-arbitrary* window - the counts are uncalibrated,
#: so any narrower band is an invention. Use +/- to tighten it by eye, or
#: ``--auto`` for a one-shot percentile window.
#:
#: This deliberately does NOT vary by lens mode. Two per-mode windows were tried
#: and were wrong twice over: they were guessed, and - since the raw stream is
#: statistically identical in both modes (measured 0.9 counts apart) - there is
#: no reason for them to differ. Worse, the close-mode window's floor was 300
#: lower, which mapped identical data 12% brighter and clipped the top of the
#: range, painting a featureless red wall.
DEFAULT_SPAN = (0, OUT_OF_RANGE)

#: Percentiles used by ``--auto``: one sample at startup, then held, so the
#: display does not shimmer the way per-frame stretching does.
AUTO_PERCENTILES = (2.0, 98.0)


def find_node() -> str:
    """The capture node, preferring the stable by-id path.

    Node numbers shift between replugs, so resolve by id first and fall back to
    scanning /dev/video* for the one that accepts the format we need.
    """
    from glob import glob
    for pattern in ("/dev/v4l/by-id/usb-*Terabee*video-index0",
                    "/dev/v4l/by-id/usb-*3Dcam*video-index0"):
        for path in sorted(glob(pattern)):
            if os.path.exists(path):
                return path
    return "/dev/video2"


class PayloadError(ValueError):
    """A capture buffer was not the expected 80x60x4 layout."""


def decode_payload(data: bytes) -> np.ndarray:
    """Decode a raw 19200-byte payload into a (60, 80) uint16 depth array.

    The frame counter is masked off, so the result is depth phase only.
    Values at :data:`OUT_OF_RANGE` are left in place: they mean "no reading",
    and it is the renderer's job to show them distinctly rather than the
    decoder's to invent a value.

    Kept separate from :class:`Camera` so decoding can be tested and reused
    without a camera attached.
    """
    if len(data) != FRAME_BYTES:
        raise PayloadError(
            f"payload is {len(data)} bytes, expected {FRAME_BYTES} "
            f"({WIDTH}x{HEIGHT}x4)")
    words = np.frombuffer(data, dtype="<u2", count=WIDTH * HEIGHT * 2)
    return words[1::2].reshape(HEIGHT, WIDTH) & DEPTH_MASK


# --------------------------------------------------------------------------
# V4L2 mmap capture (no dependencies beyond the kernel)
# --------------------------------------------------------------------------
def _iowr(nr: int, size: int) -> int:
    """``_IOWR('V', nr, size)``."""
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


VIDIOC_S_FMT = _iowr(5, 208)
VIDIOC_REQBUFS = _iowr(8, 20)
VIDIOC_QUERYBUF = _iowr(9, 88)
VIDIOC_QBUF = _iowr(15, 88)
VIDIOC_DQBUF = _iowr(17, 88)
VIDIOC_STREAMON = (1 << 30) | (4 << 16) | (ord("V") << 8) | 18
VIDIOC_STREAMOFF = (1 << 30) | (4 << 16) | (ord("V") << 8) | 19

#: v4l2_buffer offsets (index/type/memory; then the m union).
B_INDEX, B_TYPE, B_MEMORY, B_OFFSET, B_LENGTH = 0, 4, 60, 64, 72
BUFFERS = 4
WARMUP = 2


def _pack_buffer(buf: bytearray, index: int, memory: int = 1) -> None:
    """type = VIDEO_CAPTURE(1), memory = MMAP(1)."""
    struct.pack_into("<III", buf, B_INDEX, index, 1, 0)
    struct.pack_into("<I", buf, B_TYPE, 1)
    struct.pack_into("<I", buf, B_MEMORY, memory)


class Camera:
    """Streams raw frames from the 3Dcam over V4L2.

    Use as a context manager. :meth:`read` returns the masked depth array;
    :meth:`read_raw` returns both words unmasked, for diagnostics.
    """

    def __init__(self, node: str | None = None):
        self.node = node or find_node()
        self.fd = -1
        self._maps: list[mmap.mmap] = []

    def __enter__(self) -> "Camera":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> "Camera":
        self.fd = os.open(self.node, os.O_RDWR | os.O_NONBLOCK)

        # Ask for the only format offered. The driver calls it 160x60 YUYV
        # (19200 bytes); the real layout is 80x60x4, which we decode ourselves.
        fmt = bytearray(208)
        struct.pack_into("<I", fmt, 0, 1)
        struct.pack_into("<IIIII", fmt, 8, 160, 60,
                         struct.unpack("<I", b"YUYV")[0], 1, 0)
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt, True)

        rb = bytearray(20)
        struct.pack_into("<III", rb, 0, BUFFERS, 1, 1)
        fcntl.ioctl(self.fd, VIDIOC_REQBUFS, rb, True)

        for index in range(BUFFERS):
            q = bytearray(88)
            _pack_buffer(q, index)
            fcntl.ioctl(self.fd, VIDIOC_QUERYBUF, q, True)
            length = struct.unpack_from("<I", q, B_LENGTH)[0]
            offset = struct.unpack_from("<I", q, B_OFFSET)[0]
            mapping = mmap.mmap(self.fd, length, mmap.MAP_SHARED,
                                mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)
            self._maps.append(mapping)
            qb = bytearray(88)
            _pack_buffer(qb, index)
            fcntl.ioctl(self.fd, VIDIOC_QBUF, qb, True)

        so = bytearray(4)
        struct.pack_into("<I", so, 0, 1)
        fcntl.ioctl(self.fd, VIDIOC_STREAMON, so, True)
        for _ in range(WARMUP):
            self._grab()
        return self

    def close(self) -> None:
        if self.fd >= 0:
            try:
                so = bytearray(4)
                struct.pack_into("<I", so, 0, 1)
                fcntl.ioctl(self.fd, VIDIOC_STREAMOFF, so, True)
            except OSError:
                pass
        for mapping in self._maps:
            mapping.close()
        self._maps.clear()
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _grab(self, timeout: float = 5.0) -> bytes:
        """Dequeue one frame and requeue it immediately."""
        if self.fd < 0:
            raise RuntimeError("Camera is not open; call open() or use it as "
                               "a context manager")
        buf = bytearray(88)
        _pack_buffer(buf, 0)
        if not select.select([self.fd], [], [], timeout)[0]:
            raise TimeoutError(f"no frame from {self.node} within {timeout}s")
        fcntl.ioctl(self.fd, VIDIOC_DQBUF, buf, True)
        index = struct.unpack_from("<I", buf, B_INDEX)[0]
        data = bytes(self._maps[index][:FRAME_BYTES])
        qb = bytearray(88)
        _pack_buffer(qb, index)
        fcntl.ioctl(self.fd, VIDIOC_QBUF, qb, True)
        return data

    def read_raw(self) -> tuple[np.ndarray, np.ndarray]:
        """``(depth_word, other_word)`` as (60, 80) uint16, **unmasked**."""
        words = np.frombuffer(self._grab(), dtype="<u2",
                              count=WIDTH * HEIGHT * 2)
        return words[1::2].reshape(HEIGHT, WIDTH), words[0::2].reshape(HEIGHT, WIDTH)

    def read(self) -> np.ndarray:
        """One depth frame: counter masked, :data:`OUT_OF_RANGE` kept as-is."""
        return decode_payload(self._grab())

    def stats(self, seconds: float = 2.0) -> dict:
        """Summary of ``seconds`` of frames, excluding out-of-range pixels."""
        frames, counters = [], set()
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            raw = self.read_raw()[0]
            counters.update(np.unique(raw >> COUNTER_SHIFT).tolist())
            frames.append(raw & DEPTH_MASK)
        if not frames:
            return {}
        stack = np.stack(frames)
        valid = stack[stack < OUT_OF_RANGE]
        return {
            "frames": len(frames),
            "fps": len(frames) / (time.perf_counter() - t0),
            "counters": sorted(counters),
            "valid_pct": 100.0 * valid.size / stack.size if stack.size else 0.0,
            "sat_pct": 100.0 * float((stack >= OUT_OF_RANGE).mean()),
            "min": int(stack.min()), "max": int(stack.max()),
            "mean": float(stack.mean()),
            "centre_median": int(np.median(stack[:, HEIGHT // 2, WIDTH // 2])),
            "p05": int(np.percentile(valid, 5)) if valid.size else 0,
            "p95": int(np.percentile(valid, 95)) if valid.size else 0,
        }


def channels(fd_node: str | None = None) -> int:
    """Raw-channel inspector: every field of the 4-byte record, side by side.

    Something to look at when decoding is in doubt: which panel follows the
    scene, and which changes on its own.
    """
    scale = 4
    with Camera(fd_node) as cam:
        window = "raw channels  [a]uto scale  [c]olour  [r]otate  [q]uit"
        cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
        auto, jet, rot = True, False, True
        print("panels (bright = higher raw value):")
        print("  w1&0xFFF  the depth phase (what the viewer shows)")
        print("  w1>>12    the frame counter - only 0..3, never data")
        print("  w0        the other word; 0..13 only, NOT a second phase")
        t0, n = time.perf_counter(), 0
        try:
            while True:
                w1, w0 = cam.read_raw()
                n += 1
                elapsed = time.perf_counter() - t0
                panels = [("w1&0xFFF", w1 & DEPTH_MASK),
                          ("w1>>12", w1 >> COUNTER_SHIFT),
                          ("w0", w0),
                          ("w0&0xFFF", w0 & DEPTH_MASK)]
                tiles = []
                for name, arr in panels:
                    a = arr.astype(np.float32)
                    lo, hi = (float(a.min()), float(a.max())) if auto else (0.0, 4095.0)
                    idx = np.clip((a - lo) * (255.0 / max(1.0, hi - lo)), 0,
                                  255).astype(np.uint8)
                    img = (cv2.applyColorMap(idx, cv2.COLORMAP_JET) if jet
                           else cv2.cvtColor(idx, cv2.COLOR_GRAY2BGR))
                    if rot:
                        img = img[::-1, ::-1]
                    img = cv2.resize(img, (WIDTH * scale, HEIGHT * scale),
                                     interpolation=cv2.INTER_NEAREST)
                    img = add_header(img, [
                        name,
                        f"{arr.min()}..{arr.max()}",
                        f"centre {int(arr[HEIGHT // 2, WIDTH // 2])}",
                    ])
                    tiles.append(img)
                grid = np.hstack(tiles)
                grid = add_header(grid, [
                    f"{n / elapsed:4.1f} fps  "
                    f"{'auto' if auto else 'true 0..4095'}  "
                    f"{'jet' if jet else 'gray'}  "
                    f"w0 is NOT a second phase"])
                cv2.imshow(window, grid)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key == ord("a"):
                    auto = not auto
                if key in (ord("c"), ord("g")):
                    jet = not jet
                if key == ord("r"):
                    rot = not rot
        finally:
            cv2.destroyAllWindows()
    return 0


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def to_index(depth: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """Raw counts to a 0-255 index over ``[lo, hi]``.

    Higher counts are nearer, so the top of the range lands at 255.
    """
    span = max(1, hi - lo)
    return np.clip((depth.astype(np.float32) - lo) * (255.0 / span),
                   0, 255).astype(np.uint8)


def render(depth: np.ndarray, lo: int, hi: int, *, colour: bool,
           rotation: int = ROTATION, scale: int = 8,
           mark_invalid: bool = True) -> np.ndarray:
    """Colour or grayscale view of a depth frame, scaled up.

    Out-of-range pixels are painted black when ``mark_invalid``: they are "no
    reading", and showing them at a saturated colour would read as "nearest".
    """
    idx = to_index(depth, lo, hi)
    img = (cv2.applyColorMap(idx, cv2.COLORMAP_JET) if colour
           else cv2.cvtColor(idx, cv2.COLOR_GRAY2BGR))
    if mark_invalid:
        img[depth >= OUT_OF_RANGE] = (0, 0, 0)
    if rotation:
        img = img[::-1, ::-1]
    return cv2.resize(img, (WIDTH * scale, HEIGHT * scale),
                      interpolation=cv2.INTER_NEAREST)


def auto_span(frames: np.ndarray,
              percentiles: tuple[float, float] = AUTO_PERCENTILES
              ) -> tuple[int, int]:
    """A display window from the data: percentiles over valid pixels.

    Out-of-range pixels are excluded, otherwise a few dropouts would stretch the
    window to the full 12 bits and flatten everything.
    """
    valid = frames[frames < OUT_OF_RANGE]
    if valid.size < 32:
        return DEFAULT_SPAN
    lo, hi = np.percentile(valid, percentiles)
    return int(lo), max(int(hi), int(lo) + 1)


def _label(img: np.ndarray, lines: list[str], y0: int = 16) -> None:
    """Draw status lines in white, in place.

    One :func:`cv2.putText` per line, no outline. An earlier version drew a
    thick black outline underneath the white text to keep it legible over
    bright depth pixels, but at this font size the glyph strokes are about one
    pixel wide, so the black dominated the letterforms and read as doubled
    text. Status text now goes in a header bar instead - see :func:`add_header`
    - so it never has to fight the image for contrast.
    """
    y = y0
    for text in lines:
        cv2.putText(img, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (255, 255, 255), 1, cv2.LINE_AA)
        y += LINE_HEIGHT


#: Text line spacing and header bar styling.
LINE_HEIGHT = 18
HEADER_BG = (28, 28, 34)


def add_header(img: np.ndarray, lines: list[str]) -> np.ndarray:
    """Stack a dark header bar above ``img`` with ``lines`` drawn in it.

    Keeps status text off the depth image, so it stays readable whatever the
    frame happens to contain - no outline hacks, no contrast problems over
    bright near-field pixels.
    """
    if not lines:
        return img
    pad_top = 4
    height = pad_top + LINE_HEIGHT * len(lines)
    bar = np.full((height, img.shape[1], 3), HEADER_BG, np.uint8)
    _label(bar, lines, y0=pad_top + LINE_HEIGHT - 5)
    return np.vstack([bar, img])


# --------------------------------------------------------------------------
# Lens mode (optional: needs the vendor SDK)
# --------------------------------------------------------------------------
def _lens_mode_module():
    """Import :mod:`lens_mode`, or return ``None`` if unavailable."""
    try:
        import lens_mode
    except ImportError:
        return None
    return lens_mode


def cmd_modes(args: argparse.Namespace) -> int:
    """Print the current lens mode and where its config lives."""
    mod = _lens_mode_module()
    if mod is None:
        print("lens_mode module not found", file=sys.stderr)
        return 1
    try:
        with mod.LensMode() as lens:
            current = lens.get()
            print(f"lens mode    : {current} ({mod.mode_name(current)}, "
                  f"{mod.range_text(current)})")
            print(f"property 304 : supported={lens.supported}")
    except mod.LensModeError as exc:
        print(f"could not read lens mode: {exc}", file=sys.stderr)
        print("  - is LD_LIBRARY_PATH set to the SDK Bin directory?",
              file=sys.stderr)
        print("  - is the vendor udev rule installed? (_downloads/udev/)",
              file=sys.stderr)
    for mode in sorted(mod.MODES):
        print(f"  lens_mode {mode} = {mod.mode_name(mode):6s} "
              f"{mod.range_text(mode)}")
    config_mode, path = mod.read_config_mode()
    print(f"config       : {path or '(none found)'}"
          + (f"  lens_mode={config_mode}" if config_mode is not None else ""))
    return 0


def open_mode(request: str | None):
    """Open a lens-mode session and apply ``request``, or return ``None``.

    **The session must stay open while streaming.** The driver reverts
    ``lens_mode`` to ``ModuleConfig.json`` the moment the device is closed, so
    setting the mode and then closing the handle changes nothing. Callers keep
    the returned :class:`~lens_mode.LensMode` alive for as long as they want the
    mode applied, and close it at the end.

    Returns ``(session_or_None, mode_or_None)``. Never raises: the viewer must
    still work when the vendor SDK is unavailable.
    """
    if request is None:
        mod = _lens_mode_module()          # read-only: report the live value
        if mod is None:
            return None, None
        try:
            session = mod.LensMode().open()
        except mod.LensModeError:
            return None, None
        try:
            return session, session.get()
        except mod.LensModeError:
            session.close()
            return None, None

    mod = _lens_mode_module()
    if mod is None:
        print("lens mode requested but lens_mode.py is missing", file=sys.stderr)
        return None, None
    mode = {"close": mod.CLOSE_MODE, "near": mod.CLOSE_MODE,
            "normal": mod.NORMAL_MODE, "far": mod.NORMAL_MODE,
            "0": mod.CLOSE_MODE, "1": mod.NORMAL_MODE}.get(request.lower())
    if mode is None:
        print(f"unknown mode {request!r}; use close or normal", file=sys.stderr)
        return None, None
    try:
        session = mod.LensMode().open()
    except mod.LensModeError as exc:
        print(f"could not switch mode: {exc}", file=sys.stderr)
        print("  - is the vendor udev rule installed? (_downloads/udev/)",
              file=sys.stderr)
        return None, None
    try:
        before = session.get()
        applied = session.set(mode)
    except mod.LensModeError as exc:
        print(f"could not switch mode: {exc}", file=sys.stderr)
        session.close()
        return None, None
    note = "" if applied == mode else f" (driver kept {applied})"
    print(f"lens mode {before} -> {applied}: {mod.mode_name(applied)}, "
          f"{mod.range_text(applied)}{note}")
    if applied == before:
        print("  note: the raw stream barely differs between modes, so a "
              "change here may not be visible in this view", file=sys.stderr)
    return session, applied


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_stats(args: argparse.Namespace) -> int:
    session, mode = open_mode(args.mode)      # held open so the mode applies
    mod = _lens_mode_module()
    try:
        with Camera(args.node) as cam:
            stats = cam.stats(args.seconds or 2.0)
    finally:
        if session is not None:
            session.close()
    if not stats:
        print("no frames", file=sys.stderr)
        return 1
    name = (f"{mode} ({mod.mode_name(mode)}, {mod.range_text(mode)})"
            if mode is not None else "unknown (no SDK)")
    print(f"node          {args.node or find_node()}")
    print(f"lens mode     {name}")
    print(f"frames        {stats['frames']}  ({stats['fps']:.1f} fps)")
    print(f"counter nibble {stats['counters']}  (always a subset of 0..3)")
    print(f"depth raw     {stats['min']}..{stats['max']}  mean {stats['mean']:.0f}")
    print(f"valid p05/p95 {stats['p05']}..{stats['p95']}"
          f"   ({stats['valid_pct']:.1f}% of pixels)")
    print(f"out-of-range  {stats['sat_pct']:.2f}%  (shown black, not smoothed)")
    print(f"centre median {stats['centre_median']}   (higher = nearer)")
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    colour = not args.gray
    scale = args.scale
    rot = args.rotate
    frames = 0

    # Hold the mode session for the whole run: closing it reverts the mode.
    session, mode = open_mode(args.mode)
    mod = _lens_mode_module()
    lo = args.lo if args.lo is not None else DEFAULT_SPAN[0]
    hi = args.hi if args.hi is not None else DEFAULT_SPAN[1]
    label = f"{mod.mode_name(mode)}/{mod.range_text(mode)}" if mode is not None else "unknown"

    window = "Terabee 3Dcam 8060   [q]uit [c]olour [r]otate [+/-]range [a]uto"
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    try:
        with Camera(args.node) as cam:
            # Start the clock only now: opening the device through the vendor
            # SDK takes seconds, and --seconds should count frame time, not
            # setup time.
            t0 = time.perf_counter()
            if args.auto:
                # One sample at startup, held for the whole run: an auto window
                # that re-fits every frame would shimmer as the scene moves.
                sample = np.stack([cam.read() for _ in range(20)])
                lo, hi = auto_span(sample)
            print(f"{args.node or find_node()}: {WIDTH}x{HEIGHT} raw depth, "
                  f"window {lo}..{hi} (higher = nearer), lens mode {label}. "
                  f"{'jet' if colour else 'gray'}, q/Esc quits.")
            while True:
                depth = cam.read()
                img = render(depth, lo, hi, colour=colour, rotation=rot,
                             scale=scale)
                frames += 1
                elapsed = time.perf_counter() - t0
                c = int(depth[HEIGHT // 2, WIDTH // 2])
                invalid = int((depth >= OUT_OF_RANGE).sum())
                img = add_header(img, [
                    f"{frames / max(elapsed, 1e-6):4.1f} fps  lens {label}  "
                    f"{'jet' if colour else 'gray'}  rot {rot}",
                    f"win {lo}..{hi}  centre {c} "
                    f"({'no reading' if c >= OUT_OF_RANGE else 'ok'})  "
                    f"oor {invalid}px",
                ])
                cv2.imshow(window, img)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27):
                    break
                if key in (ord("c"), ord("g")):
                    colour = not colour
                if key == ord("r"):
                    rot = 0 if rot else ROTATION
                step = max(25, (hi - lo) // 20)
                if key in (ord("+"), ord("=")):
                    lo, hi = lo - step, hi - step
                if key == ord("-"):
                    lo, hi = lo + step, hi + step
                if key == ord("0"):
                    lo, hi = DEFAULT_SPAN
                if key == ord("a"):
                    lo, hi = auto_span(depth)
                if args.seconds and elapsed >= args.seconds:
                    break
    except TimeoutError as exc:
        print(f"stopped: {exc}", file=sys.stderr)
    finally:
        cv2.destroyAllWindows()
        if session is not None:
            session.close()
    print(f"drew {frames} frames")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terabee 3Dcam 8060 raw depth viewer and mode switch.")
    parser.add_argument("-n", "--node", default=None,
                        help="capture node (default: resolve by id)")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after N seconds; 0 = until q")
    parser.add_argument("--scale", type=int, default=8,
                        help="screen pixels per depth pixel (default 8)")
    parser.add_argument("--rotate", type=int, choices=(0, 180), default=ROTATION,
                        help="180 by default: the sensor is mounted upside down")
    parser.add_argument("--gray", action="store_true",
                        help="start in grayscale instead of jet")
    parser.add_argument("--lo", type=int, default=None,
                        help=f"raw count shown as the far end (dark); "
                             f"default {DEFAULT_SPAN[0]}")
    parser.add_argument("--hi", type=int, default=None,
                        help=f"raw count shown as the near end (bright); "
                             f"default {DEFAULT_SPAN[1]}")
    parser.add_argument("--auto", action="store_true",
                        help="fit the display window to the first frames "
                             "(percentile stretch), rather than showing the "
                             "whole raw range")
    parser.add_argument("--mode", default=None,
                        help="switch lens mode first: close|normal (needs SDK)")
    parser.add_argument("--list-modes", action="store_true",
                        help="print the current lens mode and exit")
    parser.add_argument("--stats", action="store_true",
                        help="print stream statistics and exit")
    parser.add_argument("--channels", action="store_true",
                        help="raw channel inspector")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_modes:
        return cmd_modes(args)
    if args.stats:
        return cmd_stats(args)
    if args.channels:
        return channels(args.node)
    return cmd_view(args)


if __name__ == "__main__":
    raise SystemExit(main())