#!/usr/bin/env python3
"""Minimal TeraRanger Evo reader.

Reads the sensor's 4-byte UART frames from its USB serial port and prints the
distance on one updating line. Standard library only - no pyserial required.

Frame format (verified against the live device and matching Terabee's
reference implementation in terabee_api/TerarangerBasicCommon.cpp):
    0x54 | distance_hi | distance_lo | CRC8
Distance is an unsigned 16-bit big-endian value in millimetres.
CRC8 uses polynomial 0x07 with an initial value of 0x00.
Output rate is ~118 Hz.

Three raw values are sentinels rather than distances:
    0x0000 -> too close      0xFFFF -> too far / no target
    0x0001 -> measurement failure
This framing is identical across the Evo 3m/15m/40m/60m variants; only the
usable range differs, so the same decode works for all of them."""

import glob
import sys
import termios
import time

PORT_GLOB = "/dev/serial/by-id/usb-STMicroelectronics_STM32_Virtual_ComPort*"
FALLBACK_PORT = "/dev/ttyACM0"
BAUD = 115200
HEADER = 0x54
PRINT_INTERVAL = 0.1  # seconds, so the display stays readable


def find_port():
    """Prefer the by-id symlink so the port survives replugging/reboots."""
    for path in sorted(glob.glob(PORT_GLOB)):
        return path
    return FALLBACK_PORT


def crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def open_port(path):
    try:
        fd = open(path, "rb", buffering=0)
    except FileNotFoundError:
        sys.exit("%s not found - is the TeraRanger plugged in?" % path)

    attrs = termios.tcgetattr(fd)
    attrs[0] = 0  # iflag: raw input
    attrs[1] = 0  # oflag: raw output
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag: 8N1
    attrs[3] = 0  # lflag: non-canonical
    attrs[4] = termios.B115200  # ispeed
    attrs[5] = termios.B115200  # ospeed
    attrs[6][termios.VMIN] = 1  # block until at least 1 byte
    attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIFLUSH)
    return fd


def read_frame(fd):
    """Block until one valid frame arrives, return its distance in mm."""
    buf = bytearray()
    while True:
        byte = fd.read(1)
        if not byte:
            continue
        buf += byte
        if len(buf) < 4:
            continue
        if buf[0] != HEADER:  # resync to frame header
            del buf[0]
            continue
        frame = bytes(buf[:4])
        del buf[:4]
        if crc8(frame[:3]) == frame[3]:
            return (frame[1] << 8) | frame[2]


def describe(raw):
    """Turn a raw reading into text, honouring the vendor's sentinels."""
    if raw == 0x0000:
        return "  too close (below min range)"
    if raw == 0xFFFF:
        return "  no target (beyond max range)"
    if raw == 0x0001:
        return "  measurement failure"
    return "%6d mm   %6.3f m" % (raw, raw / 1000.0)


def main():
    port = find_port()
    print("reading %s at %d baud - Ctrl+C to stop" % (port, BAUD))
    fd = open_port(port)

    last_print = 0.0
    while True:
        raw = read_frame(fd)
        now = time.monotonic()
        if now - last_print >= PRINT_INTERVAL:
            print("\r%s" % describe(raw), end="", flush=True)
            last_print = now


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()