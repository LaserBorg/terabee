#!/usr/bin/env python3
"""Tests for the payload decode - the frame-counter mask above all else.

Run:  python3 -m pytest tests/     or     python3 tests/test_decode.py
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import terabee3dcam as t  # noqa: E402


def make_payload(depth_words, other_words=None, counter: int = 0) -> bytes:
    """Build a 80x60x4 payload: w0 = other word, w1 = counter<<12 | depth."""
    n = t.WIDTH * t.HEIGHT
    other_words = other_words or [0] * n
    out = bytearray()
    for i in range(n):
        out += struct.pack("<HH", other_words[i],
                           (counter << t.COUNTER_SHIFT) | depth_words[i])
    return bytes(out)


def test_frame_counter_must_be_masked():
    """The counter shifts the whole frame; masking restores the depth.

    This is the bug that made near surfaces flicker black: with the counter
    left in, every value moves up by a multiple of 4096, and because higher
    counts mean nearer, the overflow reads as "no reading".
    """
    depth_values = [1000] * (t.WIDTH * t.HEIGHT)
    for counter in (1, 2, 3):
        payload = make_payload(depth_values, counter=counter)
        words = np.frombuffer(payload, dtype="<u2")
        unmasked = words[1::2]
        assert (unmasked > t.DEPTH_MASK).all(), "counter should push past 12 bits"
        masked = unmasked & t.DEPTH_MASK
        assert (masked == 1000).all(), f"counter {counter} leaked into depth"


def test_counter_value_is_recoverable():
    """The top nibble really is the counter, and it survives masking off."""
    payload = make_payload([500] * (t.WIDTH * t.HEIGHT), counter=3)
    words = np.frombuffer(payload, dtype="<u2")
    nibbles = (words[1::2] >> t.COUNTER_SHIFT) & 0xF
    assert set(np.unique(nibbles).tolist()) == {3}


def test_other_word_is_not_a_phase():
    """w0 stays in the low counts, so it cannot unwrap the depth phase.

    An earlier viewer tried to remove a ~1 m rollover by comparing w0 against
    w1. w0 does not carry a second phase, so that approach cannot work.
    """
    other = [12] * (t.WIDTH * t.HEIGHT)
    payload = make_payload([2000] * (t.WIDTH * t.HEIGHT), other_words=other)
    words = np.frombuffer(payload, dtype="<u2")
    assert (words[0::2] == 12).all()
    assert (words[0::2] <= 31).all(), "w0 should be small, not a 12-bit phase"


def test_decode_keeps_out_of_range_marked():
    """Full-scale returns mean 'no reading' and must survive as such."""
    values = [100] * (t.WIDTH * t.HEIGHT)
    values[0] = t.OUT_OF_RANGE
    payload = make_payload(values)
    depth = t.decode_payload(payload)
    assert depth.shape == (t.HEIGHT, t.WIDTH)
    assert depth[0, 0] == t.OUT_OF_RANGE
    assert (depth != t.OUT_OF_RANGE).sum() == t.WIDTH * t.HEIGHT - 1


def test_payload_size_is_checked():
    for bad in (b"", b"\x00" * 10, b"\x00" * (t.FRAME_BYTES + 1)):
        try:
            t.decode_payload(bad)
        except t.PayloadError:
            continue
        raise AssertionError(f"expected PayloadError for {len(bad)} bytes")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print(f"\n{'FAILED' if failures else 'all tests passed'}")
    raise SystemExit(1 if failures else 0)