# firmware/ — ESP-IDF shim + example app (source-only)

This directory holds the **guest** side of esp-netsim: the ESP-IDF component that makes an
unmodified ESP-NOW application talk to the host medium instead of a real radio, plus an
example app. See `RESEARCH_LOG.md` **D-017** and `docs/QEMU_INTEGRATION.md`.

> **Not compiled in this repo's CI.** These files are C for ESP-IDF and require a Linux
> ESP-IDF + Espressif-QEMU toolchain to build (this project was developed on Windows
> without them — `RESEARCH_LOG.md` D-000). They are review-ready source.

```
components/espnet_shim/
  include/espnet_shim.h          init + node/transport config
  include/espnet_transport.h     transport backend interface (swap socket<->UART)
  espnet_shim.c                  esp_now_* overrides + JSON codec + RX task
  espnet_transport_socket.c      TCP-over-open_eth backend (len-prefixed framing)
  CMakeLists.txt                 component registration (+ optional --wrap linkage)
examples/espnow_ping/            minimal app that runs unmodified on HW and under QEMU
```

## How it maps to the protocol

`espnet_shim.c` emits exactly the messages in `docs/PROTOCOL.md` (HELLO, ADD_PEER, TX,
SLICE_DONE) and consumes TX_STATUS/RX — the same protocol `nodes/mock_node.py` speaks. So
the host medium treats a QEMU node and a MockNode identically (D-015).

## Build (Linux)

```bash
. $IDF_PATH/export.sh
cd examples/espnow_ping
idf.py set-target esp32s3
idf.py -DCONFIG_ESPNET_EMULATION=1 -DCONFIG_ESPNET_NODE_ID=1 build
```

See `docs/QEMU_INTEGRATION.md` for the full runbook and the three risk-spikes to do first.
