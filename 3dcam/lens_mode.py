"""Range-mode (``lens_mode``) switch for the 3Dcam 8060 / LIPSedge M3.

The camera has two working ranges::

    lens_mode   mode     nominal range   calibration
    0           close    0.2 - 1.2 m     0211...
    1           normal   1 - 4 m         0203...

Changing the mode is not a V4L2 control and not a sensor register write: the
driver re-selects the calibration data it uses to convert the sensor's raw
phase into distance, and re-programs the depth engine. Only the vendor's
OpenNI2 driver knows how, so this module drives ``libOpenNI2.so`` through
``ctypes``. It needs no compilation and no OpenCV.

Prerequisites
-------------
* The vendor SDK's ``libOpenNI2.so`` and driver must be findable - point
  ``LD_LIBRARY_PATH`` at the SDK's ``Bin`` directory.
* The vendor udev rule must be installed. The driver reads calibration over a
  raw USB handle, and without the rule that open fails with EACCES and the
  device will not initialise at all. See ``_downloads/udev/``.

Runtime vs persistent
---------------------
:meth:`LensMode.set` applies immediately but is **per session**: the driver
re-reads ``ModuleConfig.json`` every time the device is opened, so the value
reverts on the next open. Use :func:`set_config_mode` to change it durably.

Switching re-initialises the depth camera, so any V4L2 stream must be closed
first and reopened afterwards.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
from pathlib import Path

#: LIPS custom OpenNI device property (``LIPSNICustomProperty.h``).
LENS_MODE_PROPERTY = 304

#: ``LIPS_CONFIG_NEAR_MODE`` / ``LIPS_CONFIG_NORMAL_MODE``.
CLOSE_MODE, NORMAL_MODE = 0, 1
MODES = {CLOSE_MODE: "close", NORMAL_MODE: "normal"}
RANGES = {CLOSE_MODE: (0.2, 1.2), NORMAL_MODE: (1.0, 4.0)}

#: Terabee 3Dcam 8060 = LIPSedge M3.
USB_VENDOR_ID, USB_PRODUCT_ID = 0x2DF2, 0x0214

ONI_MAX_STR = 256
ONI_API_VERSION = 2004              # ONI_CREATE_API_VERSION(2, 4)
ONI_STATUS_OK = 0

CONFIG_PATHS = ("ModuleConfig.json", "OpenNI2/Drivers/ModuleConfig.json")


class LensModeError(RuntimeError):
    """The lens mode could not be read or changed."""


class OniDeviceInfo(ctypes.Structure):
    """``OniDeviceInfo``: three fixed 256-byte strings then two usb ids."""

    _fields_ = [("uri", ctypes.c_char * ONI_MAX_STR),
                ("vendor", ctypes.c_char * ONI_MAX_STR),
                ("name", ctypes.c_char * ONI_MAX_STR),
                ("usbVendorId", ctypes.c_uint16),
                ("usbProductId", ctypes.c_uint16)]


def library_candidates() -> list[str]:
    """Places to look for ``libOpenNI2.so``, most specific first."""
    out = [os.environ[v] for v in ("TERABEE_OPENNI", "OPENNI2_REDIST",
                                   "OPENNI2_LIB") if os.environ.get(v)]
    here = Path(__file__).resolve().parent
    for base in (here, *here.parents):
        sdk = base / "_downloads" / "sdk" / "dl-sdk-linux" / "Bin"
        if (sdk / "libOpenNI2.so").is_file():
            out.append(str(sdk / "libOpenNI2.so"))
    found = ctypes.util.find_library("OpenNI2")
    if found:
        out.append(found)
    return out + ["libOpenNI2.so"]


def load_library(path: str | None = None):
    """Return a bound ``libOpenNI2.so``, or raise :class:`LensModeError`."""
    tried = [path] if path else library_candidates()
    last: Exception | None = None
    for candidate in tried:
        try:
            lib = ctypes.CDLL(candidate)
        except OSError as exc:
            last = exc
            continue
        lib.oniInitialize.argtypes, lib.oniInitialize.restype = [ctypes.c_int], ctypes.c_int
        lib.oniGetDeviceList.argtypes = [ctypes.POINTER(ctypes.POINTER(OniDeviceInfo)),
                                         ctypes.POINTER(ctypes.c_int)]
        lib.oniGetDeviceList.restype = ctypes.c_int
        lib.oniDeviceOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        lib.oniDeviceOpen.restype = ctypes.c_int
        lib.oniDeviceClose.argtypes, lib.oniDeviceClose.restype = [ctypes.c_void_p], ctypes.c_int
        lib.oniDeviceGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        lib.oniDeviceGetProperty.restype = ctypes.c_int
        lib.oniDeviceSetProperty.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_int]
        lib.oniDeviceSetProperty.restype = ctypes.c_int
        lib.oniDeviceIsPropertySupported.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.oniDeviceIsPropertySupported.restype = ctypes.c_int
        return lib
    raise LensModeError(
        f"could not load libOpenNI2.so (tried {tried}). Install the vendor SDK "
        f"and set LD_LIBRARY_PATH to its Bin directory. Last error: {last}")


def devices(lib) -> list[dict]:
    """Every device the driver reports."""
    ptr = ctypes.POINTER(OniDeviceInfo)()
    count = ctypes.c_int(0)
    if lib.oniGetDeviceList(ctypes.byref(ptr), ctypes.byref(count)) != ONI_STATUS_OK:
        return []
    return [{"uri": ptr[i].uri.decode(), "vendor": ptr[i].vendor.decode(),
             "name": ptr[i].name.decode(),
             "usb": f"{ptr[i].usbVendorId:04x}:{ptr[i].usbProductId:04x}"}
            for i in range(count.value)]


class LensMode:
    """Open handle on the camera's lens-mode property.

    ::

        with LensMode() as lens:
            print(lens.get())        # 0 or 1
            lens.set(CLOSE_MODE)
    """

    def __init__(self, uri: str | None = None, library: str | None = None):
        self._lib = load_library(library)
        self._uri = uri
        self._device = ctypes.c_void_p()

    def open(self) -> "LensMode":
        if self._lib.oniInitialize(ONI_API_VERSION) != ONI_STATUS_OK:
            raise LensModeError("oniInitialize failed")

        uri = self._uri
        if uri is None:
            found = [d for d in devices(self._lib) if d["usb"] == "2df2:0214"]
            if not found:
                raise LensModeError("no Terabee 3Dcam found on the USB bus")
            uri = found[0]["uri"]
        rc = self._lib.oniDeviceOpen(uri.encode(), ctypes.byref(self._device))
        if rc != ONI_STATUS_OK:
            raise LensModeError(
                f"oniDeviceOpen failed (status {rc}). This is normally missing "
                "raw USB permission - install _downloads/udev/99-TERABEE-video.rules")
        return self

    def close(self) -> None:
        if self._device:
            self._lib.oniDeviceClose(self._device)
            self._device = ctypes.c_void_p()

    def __enter__(self) -> "LensMode":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def supported(self) -> bool:
        """Whether the driver exposes the property."""
        return bool(self._lib.oniDeviceIsPropertySupported(
            self._device, LENS_MODE_PROPERTY))

    def get(self) -> int:
        """Current mode: 0 = close, 1 = normal."""
        value = ctypes.c_int(-1)
        size = ctypes.c_int(ctypes.sizeof(value))
        rc = self._lib.oniDeviceGetProperty(self._device, LENS_MODE_PROPERTY,
                                            ctypes.byref(value), ctypes.byref(size))
        if rc != ONI_STATUS_OK:
            raise LensModeError(f"reading lens mode failed (status {rc})")
        return value.value

    def set(self, mode: int) -> int:
        """Switch to ``mode`` and return the read-back value.

        Re-initialises the depth camera: close any V4L2 stream first.
        """
        if mode not in MODES:
            raise LensModeError(f"mode must be one of {sorted(MODES)}, got {mode}")
        value = ctypes.c_int(mode)
        rc = self._lib.oniDeviceSetProperty(self._device, LENS_MODE_PROPERTY,
                                            ctypes.byref(value), ctypes.sizeof(value))
        if rc != ONI_STATUS_OK:
            raise LensModeError(f"setting lens mode {mode} failed (status {rc})")
        return self.get()


def config_path(start: Path | None = None) -> Path | None:
    """The ``ModuleConfig.json`` the driver would read, if it exists."""
    roots = [Path.cwd()]
    for candidate in library_candidates():
        if candidate and os.path.sep in candidate:
            roots.append(Path(candidate).resolve().parent)
    if start:
        roots.append(Path(start))
    for root in roots:
        for rel in CONFIG_PATHS:
            if (root / rel).is_file():
                return root / rel
    return None


def read_config_mode(path: Path | None = None) -> tuple[int | None, Path | None]:
    """``(lens_mode, path)`` from ``ModuleConfig.json``."""
    path = path or config_path()
    if path is None:
        return None, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LensModeError(f"could not read {path}: {exc}") from exc
    return data.get("config", {}).get("lens_mode"), path


def set_config_mode(mode: int, path: Path | None = None) -> Path:
    """Persist ``lens_mode`` in ``ModuleConfig.json``; returns the path written."""
    if mode not in MODES:
        raise LensModeError(f"mode must be one of {sorted(MODES)}, got {mode}")
    path = path or config_path()
    if path is None:
        raise LensModeError("no ModuleConfig.json found")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LensModeError(f"could not read {path}: {exc}") from exc
    data.setdefault("config", {})["lens_mode"] = mode
    try:
        path.write_text(json.dumps(data, indent=4) + "\n")
    except OSError as exc:
        raise LensModeError(f"could not write {path}: {exc}") from exc
    return path


def mode_name(mode: int | None) -> str:
    """``'close'``, ``'normal'`` or ``'?'``."""
    return MODES.get(mode, "?")


def range_text(mode: int | None) -> str:
    """``'0.2-1.2 m'`` for a mode, else ``'?'``."""
    lo_hi = RANGES.get(mode)
    return f"{lo_hi[0]}-{lo_hi[1]} m" if lo_hi else "?"


__all__ = ["CLOSE_MODE", "NORMAL_MODE", "MODES", "RANGES", "LensMode",
           "LensModeError", "OniDeviceInfo", "config_path", "devices",
           "load_library", "mode_name", "range_text", "read_config_mode",
           "set_config_mode"]