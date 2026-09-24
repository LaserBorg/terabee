#!/bin/sh
# Install the Terabee/LIPS udev rule so the camera's raw USB node is writable.
#
# Why this is needed
# ------------------
# The LIPS OpenNI2 driver (libLIPSedge-DL.so) reads the sensor's calibration
# ("FLASH") over a raw libusb handle on /dev/bus/usb/BBB/DDD, *not* through
# /dev/videoN. Without this rule that open() fails with EACCES and the driver
# reports:
#
#     ERROR  Fail to open FLASH device
#     ERROR  FLASH read failed (-5)! Please contact the camera vendor.
#     ERROR  Cannot load parameters from FLASH!
#     ERROR  Failed to initialize camera.
#
# /dev/videoN usually works without the rule because it carries a desktop ACL
# granting the logged-in user rw-, which is why plain V4L2 capture works fine
# while the SDK cannot open the device.
#
# The rule is the vendor's own, shipped as
#   Terabee_3Dcam_SDK_1.6.0.0_Linux/.../99-TERABEE-video.rules
#
# Run with:  sudo sh install-udev-rule.sh

set -e

HERE=$(cd "$(dirname "$0")" && pwd)
SRC="$HERE/99-TERABEE-video.rules"
DST=/etc/udev/rules.d/99-TERABEE-video.rules

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo sh $0" >&2
    exit 1
fi

if [ ! -f "$SRC" ]; then
    echo "Cannot find $SRC" >&2
    exit 1
fi

echo "Installing $DST"
cp -f "$SRC" "$DST"
chmod a+r "$DST"

echo "Reloading udev rules"
udevadm control --reload-rules
# Re-apply to already-connected devices so no replug is needed.
udevadm trigger --subsystem-match=usb --action=change

echo
echo "Done. Verifying..."
sleep 1
for node in /dev/bus/usb/*/*; do
    [ -e "$node" ] || continue
    if udevadm info -q property -n "$node" 2>/dev/null | grep -q 'ID_VENDOR_ID=2df2'; then
        echo "  camera USB node: $node"
        ls -la "$node"
    fi
done
echo
echo "If the permissions above are not rw for your user, unplug and replug the camera."