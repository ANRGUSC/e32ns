# esp-netsim — a Cooja-like network emulator for ESP32-S3

Run a network of **virtual ESP32-S3 nodes** on a laptop: describe a topology and link
conditions, run the same ESP-IDF firmware each node would run on real hardware, and let a
custom **wireless-link medium** decide when ESP-NOW / Wi-Fi frames are delivered, lost,
delayed, or collide. Inspired by Contiki's Cooja; built for ANRG.

> Every design decision in this repo is recorded, with rationale and sources, in
> **[`RESEARCH_LOG.md`](RESEARCH_LOG.md)** (decisions `D-000…`, validations `V-…`).

> **Honest status (see [`RESEARCH_LOG.md`](RESEARCH_LOG.md) (Appendix F — status update)).**
> Feasibility is **retired**: real ESP-IDF firmware boots on emulated ESP32-S3 (M1), a real
> ESP-NOW frame travels from firmware into the host medium (risk #2), the `esp_now_*` shim
> interposes via `--wrap` (risk #3), and the QMP barrier is validated on-target — clock-freeze
> holds, throughput passes, latency fidelity passes at a 250 µs slice (risk #1). **Not yet done:**
> a full multi-node run driving two real QEMU nodes through a shared barrier (the medium/
> orchestration layer below is unit-tested against a Python stand-in, `MockNode`), and hardware
> validation. No milestone beyond M1 is "complete." Details + evidence in the status update.

## What is built and tested (software layer, against a Python stand-in)

The host-side medium/orchestration is implemented and unit-tested in pure Python — the wireless
medium, the SliceTime-style barrier clock, the wire protocol, pcapng tracing, config, and a
CLI — driven by a built-in **mock node** that speaks the same protocol a QEMU node will. This
is a **CI/unit harness for the medium**, not a demonstration of real firmware.

```bash
pip install -e .
esp-netsim validate configs/multi_node_espnow.yaml
esp-netsim run      configs/multi_node_espnow.yaml     # writes out/*.pcapng, prints stats
```

Example output (5 nodes in a line, physical SNR→PER model):

```
transmissions  : 8  (success 8, fail 0)
receptions     : delivered 32, dropped 0
trace written  : out/multi_node_espnow.pcapng
  node 5 (aa:bb:cc:00:00:05): received 8 frames
```

Open the `.pcapng` in Wireshark: radiotap headers + synthesized ESP-NOW action frames,
each annotated with the medium's SNR / PER / loss-reason in a per-packet comment.

## The QEMU + ESP-IDF path (built and proven on-target)

`firmware/components/espnet_shim` (overrides `esp_now_*` via `--wrap`, pipes frames to the
medium) + `firmware/examples/espnet_node` (open_eth+DHCP node) + `orchestrator/qemu_launcher.py`
(QMP `stop`/`cont` gate). Runs under ESP-IDF v5.4.2 + Espressif QEMU 9.0.0 in WSL2: real firmware
boots (M1), a real ESP-NOW frame reaches the host medium (risk #2), and the barrier is validated
(risk #1). Bring-up runbook + probe scripts: **[`RESEARCH_LOG.md` Appendix D](RESEARCH_LOG.md)**
and `firmware/qemu/`.

Because both node kinds are clients of one wire protocol, a scenario validated with
mock nodes behaves identically when a node is swapped for QEMU+shim (D-002/D-015).

## Architecture at a glance

```
   MockNode / QEMU+espnet_shim   ── TCP (len-prefixed binary) ─►  MediumServer
        (virtual ESP32 nodes)                                        │
                                                                     ▼
                                            ┌──────────  Simulation (barrier clock, D-004)
                                            │            snapshot nodes → RUN_SLICE →
                                            │            collect TX → wait SLICE_DONE →
                                            ▼            process batch → dispatch RX/STATUS
                                     MediumEngine  (medium.py)
                                            │  positions, ESP-NOW semantics (D-012)
                     ┌──────────────────────┼──────────────────────┐
                     ▼                      ▼                       ▼
              pathloss (D-006)     error_model (D-007)       airtime/DCF (D-008)
              log-distance         constant | UDGM | SNR→PER  DIFS/SIFS/backoff
                                            │
                                            ▼
                                   pcapng + radiotap trace (D-016)
```

See **[`RESEARCH_LOG.md`](RESEARCH_LOG.md)** Appendix A (architecture) and Appendix B (protocol).

## Wireless-medium models

| Model      | What it does                                   | Use |
|------------|------------------------------------------------|-----|
| `constant` | fixed packet-error rate                        | M2 controlled-loss link |
| `range`    | Cooja **UDGM**: deliver inside `tx_range`      | familiar Cooja knob |
| `snr_per`  | log-distance path loss → SNR → per-modulation BER → PER | M3 physical model |

Plus 802.11 DCF airtime, SINR-vs-summed-interference collision with capture, and a
single seeded RNG for byte-reproducible runs. A first sanity check (`RESEARCH_LOG.md`
**V-001**) shows the model's emergent range (~200 m) matches published ESP-NOW LOS
range (190–250 m).

## Layout

```
src/esp_netsim/         host-side simulator (Python, tested)
  protocol.py           wire codec           channel/    pathloss, error_model, airtime
  medium.py             RadioMedium engine   orchestrator/ simulation, runner, qemu_launcher
  transport/            asyncio TCP server   nodes/      mock_node
  config/  trace/  cli.py
firmware/               ESP-IDF shim + example apps (C) + qemu/ probe scripts
configs/                example scenarios (YAML)
tests/                  pytest suite (56 tests)
docs/                   raw evidence logs (m1_qemu_boot, spike2_evidence)
RESEARCH_LOG.md         single source of truth: every decision + validation + folded reference
```

## Tests

```bash
pip install -e . pytest pytest-asyncio pyyaml
pytest -q          # 56 passing: protocol, channel physics, medium semantics, e2e, config, trace
```

## Status vs. the project milestones

**M1 done with real firmware; the three feasibility risks are retired** (shim linkage via
`--wrap`, transport out of QEMU, and the QMP barrier — clock-freeze/throughput/latency all
measured on-target). Not yet done: a full multi-node run driving real QEMU nodes through a
shared barrier (medium layer is unit-tested against `MockNode`), and hardware validation.
Full breakdown: [`RESEARCH_LOG.md` Appendix C](RESEARCH_LOG.md).
