# Terabee 3dCam 8060

A rebadged **LIPSedge M3** (LIPS Corp, TI OPT8320 ToF sensor), sold by Terabee as
the 3Dcam 80x60 / 8060. It enumerates as a UVC camera and reports 160x60 YUYV,
but the 19200-byte payload is 80x60 pixels of 4 bytes.

## Payload

Two little-endian uint16 words per pixel:

| word | bytes | meaning |
|---|---|---|
| `w0` | 0–1 | **not** a second depth channel — only reaches ~0..13 in practice |
| `w1` | 2–3 | bits 0–11 depth phase, **bits 12–15 rolling frame counter** |

Three things to know, all verified on real captures:

1. **The top 4 bits of `w1` are a frame counter, not data.** One value for the
   whole frame, cycling `0,1,2,3`. Left unmasked, one pixel reads
   `5774, 9748, 13595, 1648, …` as the counter steps — 100% of pixels on a
   counter=1 frame exceed 4095. Because higher counts mean *nearer*, the
   overflow makes near surfaces read as "no reading" and flicker black.
   **This was the "hand suddenly goes black" bug.** Mask with `& 0x0FFF`.
2. **Higher raw counts mean nearer.** Inverted vs. most depth sensors.
3. **`4094`+ means "no reading", not "very far".** Full-scale returns are
   dropouts; exclude them, don't smooth over them.

`w0` is not a second phase, so the depth phase **cannot be unwrapped** against
it — an earlier attempt to remove a rollover that way was based on a false
premise and has been dropped.

## Range modes

`lens_mode` selects a working range. It is **not** a UVC control and not a
sensor register write: the vendor driver re-selects calibration data and
re-programs the depth engine, so only the LIPS OpenNI2 driver can change it.

| `lens_mode` | mode | nominal range | calibration |
|---|---|---|---|
| 0 | close | 0.2 – 1.2 m | `0211…` |
| 1 | normal | 1 – 4 m | `0203…` |

> **Caveat — the mode switch works, but does not change the raw stream.**
> Measured with the session held open (see below), same scene:
>
> | mode | mean | p05/p95 | centre |
> |---|---|---|---|
> | 0 close | 2756 | 2377..3112 | 2839 |
> | 1 normal | 2755 | 2377..3112 | 2927 |
>
> A mean difference of **0.9 counts out of ~2750**, and the two rendered frames
> are visually identical. So `lens_mode` selects the *driver's* calibration and
> depth-engine configuration; the V4L2 payload is the raw phase and is
> mode-independent. Only the driver's own depth output (real millimetres, real
> range) would show the difference.
>
> This means the observed ~1.24 m black cutoff is **not** explained by the mode
> switch, and not by the frame counter either (that caused the flicker, above).
> It is still unexplained.

### Switching

```sh
python3 terabee3dcam.py --list-modes            # current mode + config path
python3 terabee3dcam.py --mode close --stats    # switch, hold open, measure
```

**The session must stay open while streaming.** The driver restores `lens_mode`
from `ModuleConfig.json` as soon as the device is closed, so setting the mode and
immediately closing the handle changes nothing — a fresh session reads the old
value straight back. The program therefore holds the session open for the whole
run and closes it at exit. (V4L2 capture coexists fine with an open OpenNI2
session; verified.)

For a persistent change, edit `lens_mode` in `ModuleConfig.json` (search order:
`./ModuleConfig.json`, then `./OpenNI2/Drivers/ModuleConfig.json`, then the
driver's installed directory).

### Prerequisite: the vendor udev rule

The driver reads calibration over a **raw USB handle**, not `/dev/videoN`. Without
the rule that open fails with `EACCES` and the SDK reports
`Failed to initialize camera`, while plain V4L2 capture carries on working
(because `/dev/videoN` has a desktop ACL). Install the vendor rule before using
any SDK feature:

```sh
sudo sh _downloads/udev/install-udev-rule.sh
```

## Usage

```sh
python3 terabee3dcam.py                # live viewer: q quit, c colour, r rotate, +/- range
python3 terabee3dcam.py --stats        # one-line stream statistics
python3 terabee3dcam.py --channels     # raw channel inspector (all four fields)
python3 terabee3dcam.py --seconds 5    # run for 5 s
```

Status text is drawn in a dark header bar above the frame rather than over it, so
it stays readable whatever the depth image contains. (Overlaying outlined text on
the frame looked doubled at this resolution: the black outline was thicker than
the white fill, so it showed through the letterforms.)

`--seconds` counts streaming time, not startup: opening the device through the
vendor SDK takes a few seconds, and the clock starts after the first frame.

Files:

* `terabee3dcam.py` — capture, decode, render, CLI, optional mode switch
* `lens_mode.py` — `lens_mode` read/write over OpenNI2 (pure `ctypes`, no build)
* `tests/test_decode.py` — decode tests, frame-counter mask above all
* `_downloads/` — SDKs, manuals, CAD, udev rule (git-ignored, ~253 MB)

## Local downloads

Everything below is in `_downloads/` (git-ignored).

### SDKs

| What | Path | Upstream |
|---|---|---|
| LIPSedge DL SDK 1.1.1 Linux amd64 (2026-08-13) | `_downloads/sdk/LIPSedge-DL-SDK-Linux-amd64-1.1.1_195.xz.run` | [LIPSedge SDK](https://dev.lips-hci.com/introduction-to-lipsedge-sdk-1-x/lipsedge-sdk-1-x/) |
| Terabee 3Dcam SDK 1.6.0.0 Linux | `_downloads/sdk/Terabee-3Dcam-SDK-1.6.0.0-Linux.zip` | [archive.org](https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2021/02/Terabee-3Dcam-SDK-1.6.0.0-Linux.zip) |

The `.run` is a makeself archive — extract without installing by skipping its
header (`skip="899"` for the DL one), then `xz -dc`:

```sh
tail -n +900 LIPSedge-DL-SDK-Linux-amd64-1.1.1_195.xz.run > body.bin
xz -dc body.bin | tar -x
```

### Ready-to-run lens-mode tool

`_downloads/sdk/dl-sdk-linux/` is the unpacked DL SDK subset: the `libLIPSedge-DL.so`
driver, OpenNI2 headers, `ModuleConfig.json`, and the prebuilt `CameraLensModeTest`.
It needs no OpenCV — only the bundled `libOpenNI2.so`:

```sh
cd _downloads/sdk/dl-sdk-linux/Bin
LD_LIBRARY_PATH="$PWD" ./CameraLensModeTest      # prompts: input next lens mode (0/1/2/3)
```

### Docs

| What | Path | Original |
|---|---|---|
| Terabee 3Dcam 80x60 user manual | `_downloads/docs/Terabee-3Dcam-80x60-user-manual.pdf` | [archive.org](https://web.archive.org/web/20240526214657/https://www.terabee.com/wp-content/uploads/2021/05/Terabee-3Dcam-80x60-user-manual.pdf) |
| Terabee 3Dcam 80x60 spec sheet | `_downloads/docs/Terabee-3Dcam80x60-specsheet.pdf` | [archive.org](https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2019/03/Terabee_3Dcam80x60_specsheet-1.pdf) |
| LIPSedge DL/M3 SDK user manual v1.2.1 | `_downloads/docs/LIPSedge-DL_M3-SDK-Users-Manual-v1.2.1.pdf` | [LIPS](https://s3file_list.lips-hci.com/s3file/LIPS%20Partner%20Portal/Product%20Resource/LIPSedge%20DL/Manual/LIPSedge%20DL_M3%20SDK_Users_Manual_v1.2.1%2820240807%29.pdf) |
| LIPSedge SDK release notes v1.1.1 | `_downloads/docs/LIPSedge-SDK-ReleaseNotes-v1.1.1.pdf` | [LIPS](https://s3file.lips-hci.com/s3file/LIPS%20Partner%20Portal/Product%20Resource/LIPSedge%20SDK/SDK%20v1.1.1/LIPSedge%20SDK_Release_Note_v1.1.1.pdf) |

Note: the archive has only one distinct spec sheet — `specsheet-1` and `specsheet-2`
are byte-identical there, so they are stored once.

### CAD

`_downloads/cad/3dcam_cad_files.zip` — [archive.org](https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2019/03/3dcam_cad_files.zip)

### Not fetched (already in the bundle / not needed)

* `Terabee-3Dcam-SDK-1.6.0.0-Windows.zip` — Windows build of the SDK we already have for Linux
* `Terabee-3Dcam-SDK-1.6.0.0-Linux-ARM-RPi3.zip` — armhf build, not this machine
* LIPSedge v1.1.0 Windows and the arm64 (Jetson) builds — other platforms
* OpenCV 3.4.1 source — only needed to satisfy the *Terabee* 1.6.0.0 driver; the
  LIPS 1.1.1 driver has no OpenCV dependency, so this is obsolete



## alternative: Terabee drivers and specs from archive.org

https://web.archive.org//web/20230202112349/https://www.terabee.com/shop/3d-tof-cameras/terabee-3dcam/#downloads

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2019/03/3dcam_cad_files.zip

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2019/03/Terabee_3Dcam80x60_specsheet-1.pdf

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2019/03/Terabee_3Dcam80x60_specsheet-2.pdf

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2021/02/Terabee-3Dcam-SDK-1.6.0.0-Linux-ARM-RPi3.zip

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2021/02/Terabee-3Dcam-SDK-1.6.0.0-Linux.zip

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2021/02/Terabee-3Dcam-SDK-1.6.0.0-Windows.zip

https://web.archive.org/web/20230202112349/https://terabee.b-cdn.net/wp-content/uploads/2021/05/Terabee-3Dcam-80x60-user-manual.pdf


## Installation

```bash
sudo sh 3dcam/udev/install-udev-rule.sh
```
