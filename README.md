# esp-netsim: a Cooja-like network emulator for ESP32-S3

Describe a network of ESP32-S3 nodes in a YAML file, give each node an ESP-IDF firmware
build, and run the whole experiment on one machine. Every node is its own Espressif QEMU
instance running unmodified ESP-IDF code. Instead of a radio, each node talks to a host-side
wireless medium, which decides when ESP-NOW and Wi-Fi frames are delivered, delayed, lost,
or collide, from the nodes' positions and link model. Wi-Fi stations reach the real Internet
through a virtual access point.

Every decision, measurement and known limitation is in [`RESEARCH_LOG.md`](RESEARCH_LOG.md).

## What works

| Milestone | Status | Scenario |
|-----------|--------|----------|
| M1 single node boots, UART captured | done | any scenario; logs land in `out/<name>/nodeN.log` |
| M2 two nodes, controlled delivery and loss | done, real firmware | `configs/m2_two_qemu.yaml` |
| M3 multi-node ESP-NOW with traces | done, real firmware | `configs/m3_espnow_line.yaml` |
| M4 Wi-Fi station to the real Internet | done, real firmware | `configs/m4_wifi_gateway.yaml` |
| M5 config file launches every node, collects results | done | `esp-netsim run <scenario>` |
| M6 visualization of nodes, links, packets, consoles | done (replay page per run) | `out/<name>/report.html` |

The brief's example workflow (20 nodes, firmware A on some, firmware B on others, Wi-Fi from
selected nodes) is `configs/example_20_nodes.yaml`.

Not done: validation against physical ESP32-S3 boards. Timing is quantized to the barrier
step, which QMP overhead holds at 1.5 to 4 ms of virtual time (V-010); delivery, loss and
ordering are exact.

## Run it

Mock nodes (pure Python stand-ins, any OS) exercise the medium without QEMU:

```bash
pip install -e ".[dev]"
esp-netsim run configs/two_node.yaml
pytest
```

Real firmware needs Linux or WSL2, ESP-IDF v5.4.2 and the Espressif QEMU fork:

```bash
firmware/qemu/setup_esp_idf.sh                 # one time: ESP-IDF, QEMU, libslirp
. ~/esp/esp-idf/export.sh
for app in espnow_ping espnow_pong wifi_http; do idf.py -C firmware/examples/$app build; done
pip install -e ".[qemu]"
export LD_LIBRARY_PATH=$HOME/.local/lib:$LD_LIBRARY_PATH   # only if setup fetched libslirp
esp-netsim run configs/m2_two_qemu.yaml
```

## Scenario files

```yaml
name: my_experiment
wifi_ap: { pos: [0, 0], channel: 6 }            # optional virtual access point
nodes:
  - { id: 1, mac: "aa:bb:cc:00:00:01", pos: [0, 0],  channel: 6, firmware: ../firmware/examples/espnow_ping/build }
  - { id: 2, mac: "aa:bb:cc:00:00:02", pos: [60, 0], channel: 6, firmware: ../firmware/examples/espnow_pong/build }
  - { id: 3, mac: "aa:bb:cc:00:00:03", pos: [20, 5], channel: 6, firmware: ../firmware/examples/wifi_http/build, wifi: true }
medium:
  error_model: snr_per        # or: constant, with packet_error_rate
run:
  duration_us: 5000000
  slice_us: 250
  seed: 1
```

`firmware` is an ESP-IDF build directory, relative to the scenario file. A node without
`firmware` is a mock node driven by the scenario's `traffic` list (see `configs/two_node.yaml`).

## What a run produces (`out/<name>/`)

- `report.html`: replay of the run, with node map, links coloured by delivery ratio, each frame
  animated, and each node's console
- `trace.pcapng`: every frame and every receiver's verdict; open in Wireshark (ESP-NOW as
  vendor action frames, Wi-Fi as 802.11 data frames carrying the real IP traffic)
- `results.json`: topology, totals, and one row per frame and receiver
- `nodeN.log`: each firmware node's UART output

## Bringing your own firmware

Any ESP-NOW or Wi-Fi station app that builds for esp32s3 runs unmodified. Point the project at
the shim and enable the emulated Ethernet MAC:

```cmake
set(EXTRA_COMPONENT_DIRS "<repo>/firmware/components")   # in the project CMakeLists.txt
```

```
CONFIG_IDF_TARGET="esp32s3"                              # in sdkconfig.defaults
CONFIG_ETH_USE_OPENETH=y
```

The shim takes over `esp_now_*` and the Wi-Fi bring-up calls via linker `--wrap`. It reads
each node's ID, MAC and channel from NVS, which the runner writes per node, so one build
serves every node that runs it. Build the same app with `-DESPNET_HARDWARE=1` for real boards.

## Layout

```
src/esp_netsim/
  medium.py              wireless medium: carrier sense, collisions, retries, ESP-NOW semantics
  channel/               path loss, SNR->PER, 802.11 airtime
  orchestrator/          barrier clock, scenario runner, QEMU launcher, Wi-Fi bridge
  protocol.py            node <-> medium wire format (mirrored by the C shim)
  config/ trace/ nodes/ transport/ cli.py
firmware/components/espnet_shim/   the ESP-IDF shim (C)
firmware/examples/                 firmware A (espnow_ping), B (espnow_pong), C (wifi_http)
configs/                           scenarios
tests/                             pytest suite
docs/                              raw evidence logs from the feasibility spikes
```
