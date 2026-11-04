# esp-netsim: a Cooja-like network emulator for ESP32-S3

Describe a network of ESP32-S3 nodes in a YAML file, give each node an ESP-IDF firmware
build, and run the whole experiment on one machine. Every node is its own Espressif QEMU
instance running unmodified ESP-IDF code. Instead of a radio, each node talks to a host-side
wireless medium, which decides when ESP-NOW and Wi-Fi frames are delivered, delayed, lost,
or collide, from the nodes' positions and per-link settings. Wi-Fi stations associate with a
virtual access point and reach the real Internet through it.

Every decision, measurement and known limitation is in [`RESEARCH_LOG.md`](RESEARCH_LOG.md);
its Appendix C is the current status.

## What works

| Brief milestone or requirement | Scenario or command |
|---|---|
| M1 a node boots, UART captured | any firmware scenario; `out/<name>/nodeN.log` |
| M2 two nodes, controlled delivery and loss | `configs/m2_two_qemu.yaml` |
| M3 multi-node ESP-NOW with traces | `configs/m3_espnow_line.yaml` |
| M4 Wi-Fi station to the real Internet (association, DHCP, DNS, HTTP) | `configs/m4_wifi_gateway.yaml` |
| M5 a config file launches every node and collects results | `esp-netsim run <scenario>` |
| M6 nodes, links, packets and consoles | `esp-netsim run --live` while it runs; `out/<name>/report.html` after |
| topology editing | `esp-netsim edit <scenario>` |
| per-link RSSI and loss | `links:` in a scenario |
| the 20-node example workflow | `configs/example_20_nodes.yaml` |

All of the above run with real firmware. Two physical boards checked the retry count,
send-callback timing and RTT against the emulator: delivery, loss and ordering are exact, and
timing is quantized to a barrier step of about 1 ms, during which guest code runs about 1.3x
slow (RESEARCH_LOG V-020); RTT reads 0.6–1.3 ms high.

## Run it

Mock nodes (Python stand-ins, any OS) exercise the medium without QEMU:

```bash
pip install -e ".[dev]"
esp-netsim run configs/two_node.yaml        # also: configs/multi_node_espnow.yaml
pytest
```

Real firmware needs Linux or WSL2, ESP-IDF v5.4.2 and the Espressif QEMU fork:

```bash
firmware/qemu/setup_esp_idf.sh                 # one time: ESP-IDF, QEMU, libslirp
. ~/esp/esp-idf/export.sh
for app in espnow_ping espnow_pong wifi_http espnow_txprobe; do idf.py -C firmware/examples/$app build; done
pip install -e ".[qemu]"
export LD_LIBRARY_PATH=$HOME/.local/lib:$LD_LIBRARY_PATH   # only if setup fetched libslirp
esp-netsim run configs/m2_two_qemu.yaml
```

Add `--live` to watch a run in the browser as it happens: the same page as the replay, following
the newest virtual time, served on `http://127.0.0.1:8766/` (`--port` to change). It stays up with
the final state until Ctrl+C. `esp-netsim validate <scenario>` checks a file without running it. `configs/hw_ab.yaml` and
`configs/hw_txprobe.yaml` repeat the board measurements in the emulator; `hw_ab` also needs
`idf.py -C firmware/examples/espnow_ping -B firmware/examples/espnow_ping/build_<n> -DPING_LEN=<n>
build` for n = 128 and 250.

The same apps build for a board with `idf.py -C firmware/examples/<app> -B build_hw
-DESPNET_HARDWARE=1 -DSDKCONFIG=build_hw/sdkconfig build`; flash with
`python -m esptool --chip esp32s3 --port <port> write_flash @flash_args` from `build_hw`.

## Scenario files

```yaml
name: my_experiment
wifi_ap: { pos: [0, 0], channel: 6, ssid: esp-netsim }   # optional virtual access point
nodes:
  - { id: 1, mac: "aa:bb:cc:00:00:01", pos: [0, 0],  channel: 6, firmware: ../firmware/examples/espnow_ping/build }
  - { id: 2, mac: "aa:bb:cc:00:00:02", pos: [60, 0], channel: 6, firmware: ../firmware/examples/espnow_pong/build }
  - { id: 3, mac: "aa:bb:cc:00:00:03", pos: [20, 5], channel: 6, firmware: ../firmware/examples/wifi_http/build, wifi: true }
links:                                          # optional: per-pair quality, both directions
  - { a: 1, b: 2, rssi_dbm: -70 }                 # fixed RSSI instead of the distance model
  - { a: 3, b: 0, loss: 0.2 }                     # extra per-attempt loss; 0 is the access point
medium:
  error_model: snr_per        # or: constant, with packet_error_rate
run:
  duration_us: 5000000
  slice_us: 250
  seed: 1
```

`firmware` is an ESP-IDF build directory, relative to the scenario file. A node without
`firmware` is a mock node driven by the scenario's `traffic` list (see `configs/two_node.yaml`).
Link quality comes from distance unless a pair is listed under `links`; `rssi_dbm: -120` cuts a
link entirely. Unknown keys are rejected.

`esp-netsim edit <scenario>` opens an existing scenario on a map: drag, add or delete nodes,
change firmware and medium settings, and override a pair's RSSI or loss; each link shows the
RSSI and delivery the medium predicts. Save validates and rewrites the file, keeping its leading
comment block (comments further down are not kept).

## What a run produces (`out/<name>/`)

- `report.html`: replay with the node map, links coloured by delivery ratio, each frame
  animated, and each node's console
- `trace.pcapng`: every frame and every receiver's verdict, for Wireshark (ESP-NOW as vendor
  action frames, Wi-Fi as 802.11 management and data frames carrying the real IP traffic)
- `results.json`: topology, totals, and one row per frame and receiver
- `nodeN.log`: each firmware node's UART output

## Bringing your own firmware

An app that uses ESP-NOW, or joins an AP as a Wi-Fi station, through the standard calls runs
unmodified. Point the project at the shim and enable the emulated Ethernet MAC:

```cmake
set(EXTRA_COMPONENT_DIRS "<repo>/firmware/components")   # in the project CMakeLists.txt
```

```
CONFIG_IDF_TARGET="esp32s3"                              # in sdkconfig.defaults
CONFIG_ETH_USE_OPENETH=y
```

The shim takes over the `esp_now_*` calls and the Wi-Fi bring-up (`esp_wifi_init`, `start`,
`set_config`, `connect` and a few more; the full list is in its `CMakeLists.txt`) via linker
`--wrap`. Other Wi-Fi calls reach the real, uninitialized driver. A station's
`esp_wifi_connect()` runs probe, authentication and association with the virtual AP over the
modeled link, then posts `WIFI_EVENT_STA_CONNECTED` and starts DHCP, or posts
`WIFI_EVENT_STA_DISCONNECTED` with the ESP-IDF reason code. The shim reads each node's ID, MAC,
channel and medium port from NVS, which the runner writes per node, so one build serves every
node that runs it.

## Layout

```
src/esp_netsim/
  medium.py              wireless medium: carrier sense, collisions, retries, ESP-NOW semantics
  channel.py             path loss, SNR->PER, 802.11 airtime
  protocol.py            node <-> medium wire format (mirrored by the C shim)
  orchestrator/          barrier clock, scenario runner, QEMU launcher, Wi-Fi bridge
  editor.py, editor.html topology editor (esp-netsim edit)
  config/ trace/ nodes/ transport/ cli.py
firmware/components/espnet_shim/   the ESP-IDF shim (C)
firmware/examples/                 firmware A (espnow_ping), B (espnow_pong), C (wifi_http),
                                   D (espnow_txprobe, send-callback timing for board comparison),
                                   E (cpu_probe, guest CPU time lost to the barrier, V-020)
configs/                           scenarios
tests/                             pytest suite
docs/                              raw evidence logs from the feasibility spikes
```
