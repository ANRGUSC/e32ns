#!/usr/bin/env bash
# Boot an ESP-IDF esp32s3 build under the Espressif QEMU fork and capture UART.
# Proven working (M1) on WSL2 Ubuntu 24.04 with ESP-IDF v5.4.2 + qemu 9.0.0.
#
# Usage:  ./run_qemu.sh <project_dir> [seconds]
# Example: ./run_qemu.sh ~/hw 15
#
# Non-obvious gotchas this encodes (see RESEARCH_LOG.md D-020):
#   * The Espressif qemu-xtensa needs libslirp.so.0. If you can't `sudo apt install
#     libslirp0`, fetch it without root:  see setup_esp_idf.sh (LD_LIBRARY_PATH trick).
#   * ESP32-S3's ROM reads the 2nd-stage bootloader at flash offset 0x0 (NOT 0x1000,
#     which is the ESP32/-S2 convention). merge_bin must place it at 0x0.
#   * Always `idf.py set-target esp32s3` BEFORE build; a silently-failed set-target
#     leaves the project on the default esp32 target -> "Invalid chip id. Expected 9
#     read 0" in QEMU.
set -euo pipefail

PROJECT_DIR="${1:?usage: run_qemu.sh <project_dir> [seconds]}"
SECONDS_CAP="${2:-15}"

: "${IDF_PATH:?source \$IDF_PATH/export.sh first}"
export LD_LIBRARY_PATH="${HOME}/.local/lib:${LD_LIBRARY_PATH:-}"

QBIN="$(find "${HOME}/.espressif/tools/qemu-xtensa" -name qemu-system-xtensa | head -1)"
[ -x "$QBIN" ] || { echo "qemu-system-xtensa not found; run setup_esp_idf.sh"; exit 1; }

cd "${PROJECT_DIR}/build"
FLASH=flash_qemu.bin
esptool.py --chip esp32s3 merge_bin -o "$FLASH" --fill-flash-size 4MB \
  0x0     bootloader/bootloader.bin \
  0x8000  partition_table/partition-table.bin \
  0x10000 "$(ls *.bin | grep -vE 'bootloader|partition|flash' | head -1)"

echo "=== booting ${PROJECT_DIR} on emulated esp32s3 (${SECONDS_CAP}s) ==="
timeout "${SECONDS_CAP}" "$QBIN" -nographic -machine esp32s3 \
  -drive file="$FLASH",if=mtd,format=raw -no-reboot
