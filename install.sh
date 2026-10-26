#!/bin/bash
# Install the Auto Tune plugin into a Klipper installation.
#
# Usage: ./install.sh [klipper_dir] [config_dir]
#   klipper_dir defaults to ~/klipper
#   config_dir  defaults to ~/printer_data/config
set -e

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KLIPPER_DIR="${1:-${HOME}/klipper}"
CONFIG_DIR="${2:-${HOME}/printer_data/config}"

if [ ! -d "${KLIPPER_DIR}/klippy/extras" ]; then
    echo "Klipper not found in ${KLIPPER_DIR}" >&2
    exit 1
fi

echo "Linking autotuner.py into ${KLIPPER_DIR}/klippy/extras"
ln -sf "${SRC_DIR}/autotuner.py" "${KLIPPER_DIR}/klippy/extras/autotuner.py"

# Keep the link out of Klipper's git status
EXCLUDE="${KLIPPER_DIR}/.git/info/exclude"
if [ -f "${EXCLUDE}" ] && ! grep -q "klippy/extras/autotuner.py" "${EXCLUDE}"; then
    echo "klippy/extras/autotuner.py" >> "${EXCLUDE}"
fi

if [ -d "${CONFIG_DIR}" ]; then
    for f in autotuner.cfg autotuner_menu.cfg; do
        if [ -e "${CONFIG_DIR}/${f}" ]; then
            echo "${CONFIG_DIR}/${f} already exists, not overwriting"
        else
            echo "Copying ${f} to ${CONFIG_DIR}"
            cp "${SRC_DIR}/${f}" "${CONFIG_DIR}/${f}"
        fi
    done
else
    echo "Config dir ${CONFIG_DIR} not found, copy autotuner.cfg and"
    echo "autotuner_menu.cfg next to your printer.cfg yourself."
fi

echo
echo "Add to printer.cfg:"
echo "  [include autotuner.cfg]"
echo "  [include autotuner_menu.cfg]   # if you have an LCD"
echo "then restart Klipper: sudo systemctl restart klipper"
