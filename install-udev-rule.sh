#!/bin/sh
# Install the udev rule granting the current user access to the BW600 HID device.
set -e
cd "$(dirname "$0")"
sudo install -m 0644 70-atorch-bw600.rules /etc/udev/rules.d/70-atorch-bw600.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --action=change
echo "Rule installed. If access still fails, unplug and replug the BW600."
