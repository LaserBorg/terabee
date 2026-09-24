# Terabee 3dCam 8060

The 3Dcam 8060 is a rebadged LIPSedge M3 LED camera module (based on TI OPT8320 sensor)

https://www.lips-hci.com/lipsedge-m3-led

SDK: 
https://dev.lips-hci.com/introduction-to-lipsedge-sdk-1-x/lipsedge-sdk-1-x/

v1.1.0 windows, v1.1.1 linux



## Range Mode

range mode is not a UVC register write — it's a configuration + calibration switch:

lens_mode	Mode	Range	    Calibration set
0	        Close	0.2 – 1.2 m	0211…
1           Normal	1 – 4 m	    0203…

### Switching modes

Edit `lens_mode` in `ModuleConfig.json`, then restart the viewer. The driver picks
it up on the next open (it re-initialises the depth camera on switch).

```
lens_mode = 0   close range   0.2 – 1.2 m
lens_mode = 1   normal range  1 – 4 m
```

`ModuleConfig.json` search order — the driver tries a local copy first, then the
installed path:

* `./ModuleConfig.json` next to the running binary
* `./OpenNI2/Drivers/ModuleConfig.json` (alongside `libLIPSedge-DL.so`)
* `/usr/etc/TERABEE/lib/ModuleConfig.json` (installed path, needs root)

Runtime switching (no restart) is also available via the OpenNI2 device property:

```cpp
device.setProperty<int>(LIPS_DEVICE_CONFIG_LENS_MODE /* 304 */, 1 /* normal */);
```

A prebuilt Linux tool for this is `CameraLensModeTest` — see `_downloads/`.

## Local downloads

Everything below is already fetched into `_downloads/` (git-ignored, ~253 MB).

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