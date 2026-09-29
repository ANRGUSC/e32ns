#!/usr/bin/env bash
# One-shot, sudo-free ESP-IDF + Espressif-QEMU setup for esp32s3, proven on WSL2
# Ubuntu 24.04 (M1). See RESEARCH_LOG.md D-020 for why each step is here.
#
# Run this INSIDE a persistent shell (not a fire-and-forget `wsl.exe -e ... &`, which
# WSL tears down when the launching process exits — D-020).
set -euo pipefail

IDF_VER="v5.4.2"          # >=5.4.2 also enables ESP-NOW v2.0 (1470-byte) payloads
mkdir -p ~/esp && cd ~/esp

# 1) ESP-IDF (shallow clone; submodules are large).
[ -d esp-idf/.git ] || git clone -b "$IDF_VER" --depth 1 --recursive \
  --shallow-submodules https://github.com/espressif/esp-idf.git
cd ~/esp/esp-idf
./install.sh esp32s3

# 2) ESP-IDF on Linux does NOT install cmake/ninja; add them to the IDF python env
#    without sudo (pip provides real cmake/ninja binaries on PATH after export.sh).
. ./export.sh
python3 -m pip install -q cmake ninja

# 3) The prebuilt qemu-xtensa needs libslirp.so.0. If `sudo apt install libslirp0`
#    is available, use it. Otherwise fetch it WITHOUT root:
if ! ldconfig -p 2>/dev/null | grep -q libslirp.so.0; then
  mkdir -p ~/.local/lib ~/.local/slirp && cd ~/.local/slirp
  apt-get download libslirp0                    # no sudo needed
  dpkg -x libslirp0_*.deb extracted
  cp "$(find extracted -name 'libslirp.so.0*' -type f | head -1)" ~/.local/lib/
  ( cd ~/.local/lib && ln -sf libslirp.so.0.* libslirp.so.0 )
fi
export LD_LIBRARY_PATH="${HOME}/.local/lib:${LD_LIBRARY_PATH:-}"

# 4) Install + verify QEMU (verification runs qemu --version, needs the lib above).
cd ~/esp/esp-idf
python3 tools/idf_tools.py install qemu-xtensa
QBIN="$(find ~/.espressif/tools/qemu-xtensa -name qemu-system-xtensa | head -1)"
"$QBIN" --version | head -1

echo "SETUP_OK. Add to your shell rc:"
echo "  export LD_LIBRARY_PATH=\$HOME/.local/lib:\$LD_LIBRARY_PATH"
echo "  . ~/esp/esp-idf/export.sh"
