# Research Log — ESP32-S3 Cooja-like Network Emulator

This log records every design decision (`D-NNN`) and validation or measurement (`V-NNN`),
newest at the bottom. IDs are stable, so code and the README cite them. Part I is the
chronological record; entries that were later reversed or superseded are kept as short stubs
pointing at what replaced them. Part II is the current-state reference: architecture, wire
protocol, milestone status, runbook and validation plan.

Sources: `../ESP32_Network_Emulator_Project_Description.pdf` (the professor's brief, "the PDF")
and `../compass_artifact_wf-...text_markdown.md` (the prior research brief, "the brief";
*brief §N* cites its numbered Key Findings sections).

---

# PART I — Decisions and measurements

## D-000 — Framing: build the host-side core first, QEMU path source-only

Reversed by D-019. With `qemu-system-xtensa` and ESP-IDF not installed on this Windows machine,
the first session built the medium, orchestration and tracing as a standalone Python package
exercised by a pure-Python `MockNode`, and left the QEMU shim as unrun source. D-019 found WSL2
could run the real toolchain, and D-020 booted firmware. The protocol seam and MockNode survive
(D-002, D-015) as the medium's test harness, not as milestone evidence.

---

## D-001 — Language and concurrency: Python + asyncio

**Decision.** Python 3.11 with `asyncio` for orchestrator, transport and medium, stdlib-only core.

**Rationale.** The brief recommends it to match ANRG's PyWiSim/ncsim conventions (brief §9, §10).
The per-slice workload is small (N frames per barrier) and I/O-bound, so Python is fast enough for
tens of nodes. If the medium becomes the bottleneck at hundreds of nodes, the N×N path-loss/SINR
hot path is isolated in `channel/` and can be vectorized or ported.

---

## D-002 — Decouple medium from QEMU behind an explicit wire protocol

**Context.** How do frames get from a QEMU-emulated node to the host medium? The brief (§2) lists
five interception points: (i) a QEMU Wi-Fi NIC model (reverse-engineer S3 radio registers; only
exists for classic ESP32); (ii) replacing the closed Wi-Fi blobs (brittle, version-locked);
(iii) an ESP-IDF component overriding `esp_now_*`/`esp_wifi_*` and piping frames to a host
transport (recommended); (iv) `esp_wifi_80211_tx` + promiscuous RX (still needs the missing radio);
(v) linker `--wrap` interposition, a mechanism for (iii).

**Decision.** (iii)+(v): a host-side medium process with a stable wire protocol; inside the node
`espnet_shim` takes over the `esp_now_*` symbols and sends frames to the medium. The protocol is
the single contract; QEMU nodes and `MockNode` are interchangeable clients.

**Rationale.** Keeps full firmware fidelity (app + FreeRTOS + lwIP + the ESP-NOW API contract),
which is the point of the project (brief §6; PDF Core Idea), without months of radio
reverse-engineering. An explicit protocol makes the medium testable on its own.

**Consequences.** Real MAC/PHY behavior is sacrificed, as the PDF's Scope Principle allows; MAC
semantics (ACK, retry, collision, airtime) are reproduced in the medium instead.

---

## D-003 — Transport: length-prefixed TCP over loopback

**Decision.** TCP to the medium on 127.0.0.1, one connection per node, each message prefixed by a
4-byte big-endian length. The medium is the long-lived listener and nodes are clients.

**Rationale.** Works the same on Windows, Linux and macOS and in CI. Making the medium the stable
server with explicit message boundaries avoids the reconnect fragility the brief notes for QEMU
`-netdev socket` TCP mode. Length framing is safe for arbitrary binary payloads. The per-node
head-of-line ordering TCP adds is harmless because a node's own frames are ordered anyway.

On QEMU the guest reaches this listener as `10.0.2.2` through open_eth and slirp NAT (V-004).
The body went from JSON to binary in V-005.

---

## D-004 — Shared virtual time via a SliceTime-style barrier (not -icount)

**Context.** N independent QEMU processes each have their own clock; Cooja's key property is one
deterministic clock across all motes (brief §4).

**Options.** `-icount` does not synchronize multiple QEMU processes and is unused on the Espressif
Xtensa S3 fork (brief §1, §4). External barrier gating via QMP `stop`/`cont` in fixed slices
(SliceTime, Renode, SimBricks) pauses every node at slice boundaries and exchanges frames while
stopped (brief §4, §6).

**Decision.** A clock master advances virtual time in slices, gating each node at the barrier.
Frames queued during a slice are processed while every node is paused, then all are released.
QEMU nodes are gated by QMP `stop`/`cont`, mock nodes by RUN_SLICE/SLICE_DONE messages. The
starting default slice was 1 ms (brief §9); firmware scenarios now use 250 µs (D-025), the
mock-only ones still 1 ms.

**Rationale.** Bounds the skew any node perceives to one quantum, sidesteps both `-icount`
problems, and avoids the EmuLink trap where an external emulator on the wall clock made Cooja
non-deterministic (brief §4).

**Risk.** Brief §9 risk #1: can a QMP barrier hold ESP-NOW latency (~1–2 ms) without wrecking
throughput? Measured in V-007, V-008, V-010 and D-035.

---

## D-005 — Virtual time is integer microseconds

Virtual time is an `int` count of microseconds from 0. Integer time avoids float drift across
millions of slices (determinism is a project goal), and µs resolution represents SIFS (10 µs),
slot (20 µs), DIFS (50 µs) and airtime exactly (brief §3, §5). Model outputs are rounded to whole
µs only when an event is scheduled.

---

## D-006 — Path loss: log-distance from the free-space loss at 1 m

**Decision.** `PL(d) = PL(d0) + 10·n·log10(d/d0) + X_sigma`, with defaults n = 3.0 and
PL(d0 = 1 m) = 40.046 dB, and `Prx = Ptx + Gtx + Grx − PL(d)`. Shadowing σ defaults to 0.

**Rationale.** These are ns-3's `LogDistancePropagationLossModel` defaults the brief cites
(exponent 3.0, ReferenceLoss 40.0459 dB at 1 m, 2.4 GHz; brief §5). The reference loss was
recomputed from Friis, `20·log10(4π·d0·f/c)` at 2.4 GHz, giving 40.046 dB. Shadowing off keeps
runs reproducible. D-037 removed shadowing, antenna gains and the d0/frequency overrides,
which no scenario could set: `Prx = Ptx − 40.046 dB − 10·n·log10(d)`, clamped at 1 m.

**Consequences.** Real exponents vary (2–4); exponent, transmit power and noise figure are
scenario settings so the model can be fitted to measured PDR (Appendix E).

---

## D-007 — Error model: an SNR→PER interface with constant and physical implementations

**Decision.** An `ErrorRateModel` interface `per(ctx) -> float`, with:
1. `ConstantErrorModel(per)`: fixed packet error rate, for controlled-loss runs (M2).
2. `SnrPerErrorModel`: path loss → SNR → per-modulation BER → `PER = 1 − (1 − BER)^(8·nbytes)`.
   DBPSK for 1 Mbps (ESP-NOW); the 11 Mbps Wi-Fi link (CCK) is approximated as coherent QPSK.
   The OFDM M-QAM tables were removed in D-037: no node ever sends at those rates.

A third, Cooja-UDGM-style `RangeThresholdErrorModel`, was shipped and later deleted by D-027 as
unused.

**Rationale.** Matches the brief's staged fidelity (constant loss for M2, physical model for M3;
brief §5, §9). Keeping the interface leaves room for a Gilbert-Elliott model (brief §10d).

**Honesty note.** This is not bit-exact with ns-3 `YansErrorRateModel` (its source was not
available). It implements the standard BER formulas YANS is built on (DBPSK `0.5·e^(−Eb/N0)`,
Q-function forms for coherent BPSK/QAM) and is tested on invariants (PER in [0,1], monotone in
SNR, limits at high and low SNR) plus hand-computed points. Eb/N0 = SNR·(B/R); the absolute curve
depends on noise figure and bandwidth, so it is tunable rather than ground truth.

---

## D-008 — Airtime: 802.11 DCF closed form

**Decision.** `airtime = DIFS + backoff + PLCP + (MAC_hdr + payload + FCS)·8/rate + SIFS + ACK`,
with DSSS slot 20 µs, SIFS 10 µs, DIFS 50 µs, long PLCP preamble+header 192 µs (brief §5).
Backoff is a seeded uniform draw in `[0, CW·slot]`.

**Rationale.** These are the brief's DCF constants. A 1-byte frame at 1 Mbps comes out on the
order of 10³ µs, the same order as the measured 1,059 µs one-way latency (brief §3). Header,
preamble and rate are parameters. Board measurements later confirmed 8 µs per byte, i.e. 1 Mbps
(V-014, V-016); the ~15-byte ESP-NOW vendor header is not modeled (V-014).

---

## D-009 — Collision and capture: per-frame SINR against summed interference

**Decision.** For each candidate reception, SINR = Prx / (N + Σ P_interferers) over the overlap
window on the same channel; deliver iff SINR ≥ the capture threshold and the error model, fed
that SINR, says the frame survives. Same channel only.

**Rationale.** The brief calls ns-3's SINR-against-summed-interference approach more faithful
than Cooja UDGM's binary collision flag, and same-channel-only sufficient for v1 since ESP-NOW
peers share a channel (brief §3, §5). Capture is real Wi-Fi behavior. Adjacent-channel overlap is
future work. An O(N²) interferer sum per frame is fine at this scale.

---

## D-011 — Determinism: one seeded RNG threaded through the medium

A single `random.Random(seed)` owned by the medium, seeded from the scenario, serves every
stochastic draw (loss, backoff); no module-level `random.*` calls. A mock-only run is
then a pure function of (scenario, seed), because the barrier fixes I/O order. Runs with QEMU
nodes are not bit-reproducible (D-029). Parallelizing the medium later would need per-stream RNGs.

---

## D-012 — ESP-NOW semantics the medium reproduces

**Context.** The brief (§3) calls the send-callback result "the single most important behavior
to reproduce."

**Decision.** `MediumEngine` reproduces:
1. Send-callback verdict per TX (`TX_STATUS`): broadcast succeeds once transmitted
   (unacknowledged); unicast succeeds iff delivered and ACKed within the retry budget, else
   fails. Explicit fail causes: channel mismatch, lost on air, and (since D-034) `no_receiver`
   for a MAC no node holds.
2. RX dispatch: broadcast to every node that passes SINR/PER; unicast only to the addressed peer
   (encrypted unicast also needs the peer added with a matching LMK). Broadcast needs no
   `add_peer`.
3. Retries: unicast reattempts up to `max_retries`, each redrawing loss and adding airtime.
   The default was 3 from the brief; D-034 set it to 34 from board measurements.
4. Payload cap `ESP_NOW_MAX_DATA_LEN = 250` (v1.0).
5. Rate 1 Mbps.

**Consequences.** Encryption is modeled as policy (does the receiver hold the peer and LMK?), not
cryptographically; payload bytes pass through. Irrelevant to timing and PER.

---

## D-013 — Collision granularity: overlapping-window SINR

**Decision.** The engine tracks in-flight transmissions with their `[start, start + airtime]`
window and channel. A reception's SINR sums the power of every other same-channel transmission
whose window overlaps it. Frames in one barrier batch are evaluated against each other
symmetrically (`process_batch`) and against frames still in flight from earlier slices.
Propagation delay (~3.3 ns/m) is ignored for overlap.

**Consequences.** Whole-frame SINR: a partial overlap counts as full interference (conservative).
Per-chunk integration is future work. D-031 later made each retry check its own window, and D-034
keeps a frame in flight for all its attempts.

---

## D-014 — Barrier dispatch: deliveries computed while paused, sent at a boundary

Original decision: process a slice's frames as one batch after every node reaches the barrier,
then dispatch, so a frame sent in slice k is acted on by slice k+1 at the latest; RX and trace
timestamps carry the computed delivery time even though dispatch is quantized. D-029 (item 3)
replaced the "end of the sending slice" rule: every delivery is now held until its computed
virtual time and released at the first boundary at or after it. The batch-while-paused discipline
and exact trace timestamps remain.

---

## D-015 — MockNode as a first-class virtual node

**Decision.** `nodes/mock_node.MockNode` is a pure-Python client implementing the node half of
the protocol: HELLO/WELCOME, RUN_SLICE → emit scheduled TX → SLICE_DONE, and recording RX and
TX_STATUS. It does what `espnet_shim` does on the QEMU side.

**Rationale.** Makes the medium and barrier testable in CI and gives a fast way to try
topologies. The medium cannot tell a mock from a QEMU node (D-002). MockNode does not validate
firmware fidelity; that is the QEMU path's job. Mock and QEMU nodes share one barrier (D-026,
V-009).

---

## D-016 — Trace format: pcapng + radiotap, simulation-clock µs timestamps

**Decision.** pcapng with `LINKTYPE_IEEE802_11_RADIOTAP` (127). Each packet is a radiotap header
(Flags, Rate, Channel, dBm antenna signal) plus a synthesized 802.11 vendor-specific action frame
(Espressif OUI `18:fe:34`) carrying the ESP-NOW payload. Timestamps are µs (`if_tsresol=6`) of
virtual delivery time. Each Enhanced Packet Block has an `opt_comment` with the medium's
internals: loss reason, SNR/SINR, PER, attempts. Dropped frames are written too, with reason.
Wi-Fi frames are written as 802.11 data frames (D-032).

**Rationale.** The brief's choice (§9): Wireshark-readable with per-packet model annotations.
Simulation-clock timestamps keep timing analysis correct at any run speed (brief §7). The
synthesized frame lets Wireshark dissect addresses and the OUI; it is the right shape, not a
byte-exact ESP-NOW element. Written by hand, no third-party library.

---

## V-001 — Emergent range matches published ESP-NOW range (self-consistency only)

Two nodes, 50 trials per distance; Ptx 20 dBm, exponent 3.0, noise figure 7 dB, capture 4 dB,
50-byte payload at 1 Mbps.

| dist (m) | SNR (dB) | PER | delivered/50 |
|---|---|---|---|
| 10 | 43.5 | 0 | 50 |
| 50 | 22.5 | 0 | 50 |
| 100 | 13.5 | ~1e-212 | 50 |
| 150 | 8.2 | ~9e-62 | 50 |
| 200 | 4.5 | ~4e-25 | 50 |
| 250 | 1.6 | 1.0 (below capture) | 0 |
| 300 | −0.8 | 1.0 | 0 |

The link dies at ~200–220 m, in line with the brief's reported ESP-NOW LOS range of 190–250 m
(brief §3). The cliff is sharp because shadowing is off and capture is a hard gate. As D-019
notes, this is a units check (the inputs determine the output), not a validation; board
validation is V-014 to V-016 and Appendix E.

---

## D-017 — ESP-IDF shim: provide `esp_now_*`, JSON over transport

Superseded. The first shim provided the public `esp_now_*` symbols from a component and sent
hand-encoded JSON. D-021 showed component provision does not interpose (IDF's symbols win;
`--wrap` is required), V-005 replaced JSON with a binary body, and D-028 rewrote the shim around
packed structs with the transport folded in.

---

## D-018 — Code-review pass: three correctness fixes

After the mock path was green (54 tests), a reviewer sub-agent audited the Python core. Fixes,
each locked by a test (now in `test_medium.py` and `test_barrier.py`):
1. Phantom interference: `process_batch` registered every frame as in-flight, including frames
   rejected before transmission. Now only frames that radiated (`attempts > 0`) interfere.
   (D-034 later made a unicast to an absent MAC radiate.)
2. Stale barrier release: a late `SLICE_DONE` from slice k−1 could release slice k. Now only a
   `SLICE_DONE` whose `t` equals the current slice's `until_us` counts.
3. RX timestamp late by SIFS + ACK (~314 µs at 1 Mbps): the data frame now lands
   `ack_tail = SIFS + ACK` before the acknowledged airtime ends; the send callback still fires
   at full airtime (`AirtimeModel.ack_tail_us()`).
4. A weak-link drop is labelled `below_sensitivity`, not `collision`.

---

## D-019 — Course correction: reverse D-000, do the emulator, spike risks first

An advisor-style review made the charge, accepted here, that the project had built a simulator
and skipped the emulator. D-000 treated a missing binary as a constraint; this machine has WSL2
Ubuntu 24.04 with internet access, so ESP-IDF and Espressif QEMU install normally. The brief
(§9) says to spike the three highest-risk unknowns first: (1) QMP barrier timing, (2) frame
transport out of QEMU, (3) shim linkage over IDF. The first session had done none of them.

Accepted corrections: M2/M3/M5 are not "done" until real firmware runs them; the shim had never
been compiled; V-001 is a units check; JSON on a µs-sensitive per-frame path is a smell, so the
wire goes binary before timing anything.

New order: M1 for real (D-020), spike #3 linkage (D-021), spike #2 transport (V-004, V-005),
spike #1 barrier (V-006 to V-008), then real nodes through the medium (V-009), then M4 and
hardware. The protocol decoupling stays, now justified by the medium being out-of-process by
nature; MockNode stays as the CI harness.

---

## D-020 / V-002 — M1: real ESP-IDF firmware boots on emulated ESP32-S3

ESP-IDF v5.4.2 `hello_world` built for `esp32s3` booted under Espressif QEMU 9.0.0 in WSL2 and
printed real UART (`docs/m1_qemu_boot.log`). Setup is `firmware/qemu/setup_esp_idf.sh`.

Gotchas, each of which cost an iteration:
1. WSL tears down background jobs launched with `wsl.exe -e bash -lc '... &'`, even with
   `nohup`, when the launcher exits. Run long jobs from a persistent shell.
2. QEMU needs `libslirp.so.0` and `sudo` was unavailable: `apt-get download libslirp0`,
   `dpkg -x` into `~/.local/lib`, set `LD_LIBRARY_PATH`.
3. ESP-IDF does not install cmake/ninja on Linux: `pip install cmake ninja` into its env.
4. `idf.py qemu` rejects this QEMU (libslirp check); running `qemu-system-xtensa` directly
   works, which is what the launcher does.
5. The ESP32-S3 ROM loads the second-stage bootloader from flash offset 0x0, not 0x1000.
6. A failed `idf.py set-target esp32s3` silently leaves the project on `esp32`; QEMU then reports
   `Invalid chip id`. Check `CONFIG_IDF_TARGET`.

---

## D-021 / V-003 — Spike #3: shim linkage requires `--wrap`

1. Providing strong `esp_now_*` symbols from a component links cleanly but does not interpose:
   the map and a QEMU boot showed the app calling IDF's `esp_now_init`, which crashed
   (`LoadProhibited`) for lack of Wi-Fi init. Once IDF's archive satisfies the symbol the shim's
   copy is never pulled. Linking is not interposing.
2. Renaming the shim's functions `__wrap_esp_now_*` and adding `-Wl,--wrap=esp_now_*` (propagated
   via `target_link_libraries(${COMPONENT_LIB} INTERFACE …)`) works: the map shows
   `__wrap_esp_now_init` in the call slot and the booted app printed the shim's log line, with no
   app source changes.

**Decision.** `--wrap` is the required mechanism, enabled in
`firmware/components/espnet_shim/CMakeLists.txt`.

---

## V-004 — Spike #2: first frame out of QEMU reaches the host medium

A `connect()` that fails fast is not a working path; the transport counted as proven only once a
HELLO reached a host listener. With `esp_netif_init()` alone and no interface up, `connect` to
`10.0.2.2` failed at once. Adding the open_eth bring-up (`CONFIG_ETH_USE_OPENETH`,
`esp_eth_mac_new_openeth` + `esp_eth_phy_new_dp83848` with `autonego_timeout_ms=100`, DHCP, wait
for `IP_EVENT_ETH_GOT_IP`) under `-nic user,model=open_eth` delivered HELLO and TX to the real
`esp_netsim.protocol` codec on the host (`docs/spike2_evidence.log`). Path: `--wrap`ped
`esp_now_send` → lwIP → open_eth → slirp NAT (guest 10.0.2.15 → host 10.0.2.2) → host codec.

---

## V-005 — Binary wire body, C and Python

Replaced the JSON body with a compact big-endian binary body in `protocol.encode_body`/
`decode_body` and in the shim; the 4-byte length framing is unchanged. The layout is locked by
`tests/test_protocol.py::test_binary_wire_layout_is_locked`. Re-verified on the emulated esp32s3:
binary HELLO and TX decoded on the host with real `esp_timer` `t`, float32 rate, MACs and seq
intact. The shim compiles clean under IDF's `-Werror`. Done before any timing so JSON encoding
on the emulated core could not contaminate the numbers.

---

## D-022 — Guest virtual time: the TX `t` field is guest `esp_timer` µs

Superseded by D-029. The decision was that the guest stamps `esp_timer_get_time()` into TX `t`
and the medium uses it as virtual time, on the premise that a guest clock frozen by `stop`
accumulates exactly the virtual time granted. V-006 confirmed the freeze, but D-029/V-010 found
running guests advance at 0.85–0.96 of host time and drift apart, so the medium now stamps QEMU
frames by slice of arrival. The shim still sends `esp_timer` in `t`.

---

## V-006 — QMP `stop` freezes the guest esp_timer

A firmware logged `esp_timer_get_time()` every 200 ms; the harness paused the VM with QMP `stop`
for a known interval. Guest time advanced 0.22 s (one loop step) while wall time advanced 3.27 s:
the guest clock is stop-frozen. The first run reported a violation that turned out to be a harness
bug (the serial socket inherited a 1 s timeout and the reader died mid-pause); raw samples were
checked before believing either result. The freeze holds; the guest's rate while running is a
separate problem (D-029).

---

## D-023 — Barrier spike pass/fail criteria, fixed before running

With one real QEMU node under QMP `stop`/`cont`: (1) median one-way latency within ±20 % of the
medium's computed value; (2) at least 100 slices per second. Expectation going in: a 1 ms slice
against ~1 ms MAC latency would be tight, and 250 µs was the first knob to try.

---

## D-024 / V-007 — Barrier throughput passes D-023 over a UNIX socket

One esp32s3 node, 200 `stop`/`cont` cycles per point:

| target slice | wall/cycle | QMP overhead | slices/s |
|---|---|---|---|
| floor (0) | 745 µs | 745 µs | 1343 |
| 1000 µs | 2374 µs | 1374 µs | 421 |
| 250 µs | 1088 µs | 838 µs | 919 |

421 slices/s at 1 ms is four times the bar. Two harness artifacts nearly produced wrong numbers:
QMP over TCP stalled ~88 ms per cycle (11 slices/s) on delayed ACK, which `TCP_NODELAY` on the
client did not fix; a UNIX socket, which the launcher uses, took ~0.75 ms. And
`pkill -f qemu-system-xtensa` killed the shell running the probe, because its command line
contained that string; kill by exact name instead.

---

## V-008 / D-025 — Latency fidelity at 250 µs slices; scenarios default to 250 µs

Revised by V-010 and D-035. This modeled each slice as exactly S µs of guest time and computed the
reactive latency of 300 real frames as `ceil((t + airtime)/S)·S − t` against the medium's 908 µs
airtime for a 16-byte acknowledged unicast: 1 ms slices gave 1,384 µs (52.5 % error, fail),
250 µs gave 1,038 µs (14.3 %, pass). D-025 set the slice to 250 µs, which every firmware
scenario still uses. V-010 then measured that a slice actually advances the guests ~2 ms because they run through
the QMP round trips, so 14.3 % does not describe what firmware sees; D-035 brought the effective
quantum to 0.85–1.3 ms.

---

## D-026 — Mixed barrier: QMP-gated QEMU nodes and message-gated mocks together

Per slice the clock master releases all participants at once and waits for all: mocks by
RUN_SLICE/SLICE_DONE, QEMU nodes by `cont`, run, `stop`. With no QEMU nodes the behavior is
unchanged. This was first built as a `NodeGate` abstraction in `orchestrator/gate.py`; D-027
collapsed the protocol and D-033 removed the file, so the barrier now calls `cont`/`stop` on the
QMP clients directly. The mixed barrier itself remains (`simulation.py`).

---

## V-009 — First M2 with real firmware: QEMU node to MockNode across the barrier

A real ESP32-S3 firmware broadcasting ESP-NOW every 20 ms, QMP-gated at 1 ms slices, and a mock
receiver at 5 m shared one barrier and a lossless medium. Result: 140 frames sent, 140 delivered,
RSSI −41.0 dBm, which is the log-distance model's value for 5 m (20 dBm − 61 dB, D-006), so the
medium applied its physics to the firmware's frames rather than passing them through. Two real
nodes followed in D-028/D-029.

---

## D-027 — Deslop pass before the two-node work

Applied `CLAUDE.md`: deleted code with no caller. Removed `RangeThresholdErrorModel` and its
config (overrides D-007's three models), the JSON debug codec, `QemuNodeSpec`/`QemuNodeHandle`,
the one-implementation `PropagationLoss` and `NodeGate` protocols, several dead helpers and
validation guards, and config fields no scenario set. Docstrings cut to one line. Net: Python
3185 → 2682 lines, C 496 → 483; 56 tests (three deleted with their subjects).

---

## D-028 — One firmware image, many nodes: identity in NVS, unmodified apps

**Context.** One build must serve many nodes ("firmware A on nodes 1–10"), and the firmware
should be the same, or as close as practical, as on hardware (PDF).

**Options.** (a) Rebuild per node with a `-D` flag: 20 builds for 20 nodes. (b) Patch the app
image: breaks its checksum and SHA-256. (c) A per-node NVS partition. (d) Assign IDs by connection
order: fragile once nodes boot in parallel.

**Decision.** (c). The runner merges each node's ESP-IDF build into a 4 MB flash image and writes
an NVS partition (namespace `espnet`: `node_id`, `mac`, `channel`, `port`) at the offset the
build's partition table gives, using Espressif's `esp-idf-nvs-partition-gen`. NVS is not
hash-checked and is where a real deployment would keep identity.

The shim starts itself inside `esp_now_init` (NVS identity, open_eth + DHCP, connect, HELLO) and
`--wrap`s the Wi-Fi bring-up an ESP-NOW app makes on hardware (`esp_wifi_init`, `set_storage`,
`set_mode`, `start`, `set_channel`) into no-ops. An app written like Espressif's ESP-NOW example
builds and runs unmodified; only the project `CMakeLists.txt` adds the shim.
`-DESPNET_HARDWARE=1` builds the same app for boards.

Shim rewrite: packed structs for every wire message, transport folded into the shim, static
FreeRTOS allocation. Two bugs fixed: the receive callback had been handed raw MAC bytes cast to
`esp_now_recv_info_t*` (an app reading `info->src_addr` would crash), and the length prefix and
body went out as two `send()` calls with Nagle on; now one call with `TCP_NODELAY`.

Firmware examples: A `espnow_ping` (broadcast a timestamped ping every 100 ms, log each reply's
RTT), B `espnow_pong` (answer each ping with a unicast reply from a task, since sending from the
receive callback is discouraged on hardware).

---

## D-029 / V-010 — Guest clocks are not host time under stop/cont; stamp by slice instead

**Context.** First run of two real nodes (M2 ping/pong, 250 µs slices): the pong's reply went out
19–27 ms after the ping was delivered, and the lag grew with run time.

**Measurement 1: a slice is not 250 µs of guest time.** 6 s of virtual time took ~3,000 slices,
so each "250 µs" slice advanced the guests ~2 ms: they keep running through the QMP `cont` and
`stop` round trips. The CLI prints the effective quantum; measured then: 2.0 ms (2 nodes),
2.4 ms (5), 2.1 ms (3, Wi-Fi), 3.7 ms (20). `slice_us: 50` gave the same result, which was read
as "below the QMP floor `slice_us` does not matter"; D-035 found the floor was an asyncio sleep
and cut the quantum to 0.85–1.3 ms.

**Measurement 2: guest clocks drift from host time and from each other.** Node 1's `esp_timer`
advanced at 0.96 of the host's estimate of running time and node 2's at 0.85; by the end the two
guests were 200–350 ms apart, though both ran identical code in the same host windows. D-022's
premise does not hold across nodes. The FreeRTOS tick also falls behind `esp_timer`, so
`vTaskDelay` and log timestamps stretch (a 100 ms loop measured 130–150 ms).

**Decision.**
1. The medium stamps a QEMU node's frame with the virtual time of the slice it arrived in, not
   the guest's `t`. Sub-slice ordering is lost; every node shares one timeline.
2. Virtual time advances by the measured guest running time per slice (mean over nodes of `stop`
   reply minus `cont` reply), not by nominal `slice_us`.
3. Every delivery (RX, TX_STATUS, Wi-Fi forward) is held until its computed virtual time and
   released at the first slice boundary at or after it (replaces D-014's end-of-slice dispatch).
4. Frames reaching the host between slices (slirp is asynchronous) join the next batch instead of
   being dropped by the per-slice reset.

**Consequence.** Reactive latency is quantized to the effective slice: M2 RTT (broadcast ping,
unicast reply, 20 % loss) p50 5.6 ms, min 3.3 ms, n = 28. Delivery, loss, retries and ordering
are unaffected. Firmware runs are not bit-reproducible even with a fixed seed, because guest
timing changes the order frames reach the medium; mock runs still are.

---

## V-011 — UART on a Windows-mounted path stalls the guest

The ping's `t0`-to-send gap was 4–34 ms, all in `ESP_LOGI`: QEMU wrote UART to a file under
`/mnt/c` (a 9P mount) and the guest blocks on every UART write. UART logs now go to the run's temp
dir on the Linux filesystem and are copied to `out/` at the end; the ping takes `t0` after
logging. Median RTT went from 13.7 ms to 4.6 ms.

---

## D-030 — Real-time mode tried and rejected

Alternative to lockstep: start the VMs once and let them free-run, with the medium applying
airtime delays against the wall clock. Measured on M2: RTT p50 8.7 ms against 4.6 ms for lockstep
in the same build, and it gives up determinism of ordering. The host loop could not tick faster
than ~1.2 ms under WSL, and the guest's own processing is the same in both modes. Removed; not
kept as an option.

---

## D-031 — Carrier sense, and retries that meet their own traffic

**Context.** M3 with five real nodes: pongs 2, 3 and 4 all reply in the same slice, get the same
start stamp (D-029), and collided at node 1; nodes 3 and 4 got 16/36 and 13/36 replies through.
On hardware they hear each other and defer.

**Decision.** (1) Carrier sense: before evaluation, each frame is delayed past any earlier
same-channel frame whose sender it hears at or above −82 dBm (802.11 preamble detection). Hidden
terminals still collide. (2) Each unicast retry is checked against the traffic overlapping that
attempt's own window; before, all retries reused the first attempt's interferers.

**Result.** Replies to node 1: node 3 16/36 → 34/34, node 4 13/36 → 35/35 (20 of node 4's needed
a retry: at −88 dBm it is a hidden terminal to node 2). Tests: hidden terminals collide; senders
in range take turns; a unicast escapes a hidden-terminal collision on retry.

---

## D-032 / V-012 — M4: Wi-Fi stations reach the real Internet across the modeled link

**Context.** PDF component 5: stations associate with a virtual AP; traffic crossing the simulated
link goes through the host to the real Internet (DNS, TCP, HTTP).

**Design.**
- Guest: the shim emulates a Wi-Fi station on the open_eth NIC.
  `esp_netif_create_default_wifi_sta` returns the Ethernet netif; `esp_wifi_start` posts
  `WIFI_EVENT_STA_START`; `esp_wifi_connect` starts Ethernet; DHCP success is re-posted as
  `WIFI_EVENT_STA_CONNECTED` and `IP_EVENT_STA_GOT_IP`. Firmware C (`wifi_http`) is a standard IDF
  station app that fetches `http://example.com` every 2 s, unmodified.
- Host: QEMU's `filter-redirector` detours every Ethernet frame between the NIC and slirp through
  four UNIX-socket chardevs to the medium (`wifi_link.py`). Each frame becomes an 11 Mbps unicast
  on the station–AP link (the AP is medium node 0 and ignores ESP-NOW); if delivered it goes on
  to slirp (uplink) or the guest (downlink) at its computed time. It contends with ESP-NOW on the
  same channel with carrier sense, retries and loss. The shim's own TCP link to the medium
  (10.0.2.2:port) is exempt: it is not radio traffic.
- Trace: Wi-Fi frames are 802.11 data frames (ToDS/FromDS, LLC/SNAP), so Wireshark decodes the IP,
  TCP and HTTP inside.

**Result (`configs/m4_wifi_gateway.yaml`, AP on channel 6):**

| station | distance | RSSI | outcome |
|---|---|---|---|
| 1 | 25 m | −62 dBm | DHCP, then HTTP 200 every 2 s (21–88 ms) |
| 2 | 160 m | −86 dBm | DHCP, then HTTP 200 every 2 s; retries in some runs (up to 4 attempts) |
| 3 | 230 m | −91 dBm | every DHCP frame lost after 4 attempts; logs "no IP yet" |

(These ran with the pre-D-034 `max_retries: 3`.)

**Bugs found.** QEMU closed the filter sockets at once because `bridge()` kept only one half of
each asyncio connection and a collected `StreamWriter` closes its socket. A new app built for the
default ESP32 target boot-looped; apps now pin `CONFIG_IDF_TARGET="esp32s3"` and scenario loading
rejects builds for any other chip.

**Scope.** No 802.11 authentication or association frames: "associated" means the link is usable.
Each station has its own slirp NAT, so stations cannot reach each other at layer 2 through the AP.
Wi-Fi rate is fixed at 11 Mbps.

---

## D-033 — M5 and M6: the scenario file launches everything; each run leaves a replay

**M5.** A node with `firmware: <build dir>` boots under QEMU; one without is a mock.
`esp-netsim run` builds each firmware node's flash image, starts every QEMU paused, connects QMP
(and the Wi-Fi sockets), runs the barrier, then writes `out/<name>/`: `trace.pcapng`,
`results.json` (topology, totals, one row per frame and receiver), each node's UART log, and
`report.html`. `orchestrator/gate.py` and the one-off M2 script are gone.

**M6.** `report.html` is a self-contained page (no network, no build step) showing what the PDF
lists: nodes at their positions, links coloured by delivery ratio, each frame animated green or
red as a timeline plays or is scrubbed, and each node's console revealed in step. Console lines
are placed by the firmware's own log timestamps, which lag virtual time (D-029).

---

## V-013 — The brief's 20-node workflow, and scale

`configs/example_20_nodes.yaml`: a 4 × 5 grid at 100 m; firmware A on nodes 1–10, B on 11–18,
C on 19–20 with the AP. 20 QEMU VMs ran 5 s of virtual time in 25 s of wall time (WSL2, 20
threads), effective quantum 3.7 ms (before D-035). 694 transmissions; both Wi-Fi stations fetched
example.com; node 1 heard no pong because none is in range of its corner, while node 10 in the
middle logged 97 RTTs.

---

## V-014 / D-034 — First hardware measurement: a unicast nobody ACKs takes ~35 attempts

**Setup.** One ESP32-S3 board (rev v0.2, CH343 USB serial), ESP-IDF v5.4.2, indoors on channel 1
with ordinary home Wi-Fi around. Firmware A built with `-DESPNET_HARDWARE=1` boots unmodified and
pings every 100 ms. With one board there is nothing to answer, so what it can measure is the send
callback, which the medium models: firmware D (`espnow_txprobe`) times `esp_now_send` to its
callback, one send in flight, for a broadcast and for a unicast to `02:00:00:00:00:99` (a
registered peer no device holds), at 16, 128 and 250 bytes. The same image runs in the emulator
as `configs/hw_txprobe.yaml`.

**Result** (µs; board n ≈ 93 per row, emulator n ≈ 62):

| send | len | board min | board p50 | board p95 | emulator p50 before | after |
|---|---|---|---|---|---|---|
| broadcast | 16 | 1,118 | 1,869 | 5,737 | 1,857 | 1,983 |
| broadcast | 128 | 2,013 | 2,625 | 6,585 | 3,278 | 2,219 |
| broadcast | 250 | 2,992 | 3,615 | 6,763 | 3,681 | 3,642 |
| unicast, no ACK | 16 | 28,814 | 38,937 | 65,398 | 1,994 | 33,287 |
| unicast, no ACK | 128 | 60,397 | 74,508 | 94,391 | 3,177 | 60,215 |
| unicast, no ACK | 250 | 92,876 | 105,323 | 126,559 | 3,498 | 90,455 |

**Reading.** Broadcast cost grows 8.0 µs per byte, so ESP-NOW goes out at 1 Mbps as the model
assumes. The board adds ~400 µs per send that the airtime model does not have (driver path); the
emulator hid it under its quantum (1.5–4 ms at the time, V-010). A failed unicast grows 274 µs per
byte at the minimum and 284 at the median. Each transmission costs 8 µs per byte, so the radio
sends the frame about 35 times before reporting FAIL. The medium had two errors here. It allowed
4 transmissions (`max_retries: 3`, from the brief), and for a destination no node holds it
reported `peer_not_exist` at once without radiating. A real sender cannot know nobody is there.

**Decision.**
1. `max_retries` defaults to 34 (35 transmissions).
2. A unicast to a MAC no node holds radiates every attempt and fails as `no_receiver`.
3. A frame stays in flight for all its attempts, not just the first, so neighbours defer through
   a whole retry sequence (a failed 250-byte unicast holds the channel ~100 ms).

**Residual.** With the same airtime model, the expected time to fail is 37.0 / 68.4 / 102.6 ms.
The board measures 1.03–1.09× that; the ~15-byte ESP-NOW vendor header the model omits accounts
for about 4 ms of it. The emulator measures 0.88–0.90×: the medium schedules the callback at the
right virtual time, but the guest's `esp_timer` advances slower than the host time the barrier
credits, the drift V-010 measured (0.85–0.96). Closing that needs the guest clock itself to set
virtual time (the `-icount` item in Appendix C). V-015 later showed this residual is not
separable from channel load. Retries for Wi-Fi data frames use the same default but were not
measured; on hardware they also go through rate control.

---

## V-015 — V-014 repeated on a second board

**Setup.** With the V-014 board (MAC `d4:05:92:7b:14:fc`) unavailable, a different ESP32-S3
(rev v0.2, CH343, MAC `d4:05:92:79:90:c8`, 8 MB PSRAM) ran the same `build_hw` image of firmware
D, channel 1, on 2026-10-20. All 600 sends logged (n = 100 per row).

**Result** (µs):

| send | len | min | p50 | p95 | V-014 p50 |
|---|---|---|---|---|---|
| broadcast | 16 | 1,117 | 1,211 | 2,543 | 1,869 |
| broadcast | 128 | 2,015 | 2,221 | 3,796 | 2,625 |
| broadcast | 250 | 2,992 | 3,115 | 4,629 | 3,615 |
| unicast, no ACK | 16 | 27,887 | 32,324 | 38,955 | 38,937 |
| unicast, no ACK | 128 | 57,767 | 63,102 | 74,662 | 74,508 |
| unicast, no ACK | 250 | 88,749 | 95,103 | 104,807 | 105,323 |

**Reading.** Minima match V-014 to within 3 %, and broadcast still costs 8.1 µs per byte
(1 Mbps). Medians and tails are lower on every row, broadcast included, so the channel was quieter
during this run; that is carrier-sense deferral, not the radio. A failed unicast grows 260 µs per
byte at the minimum and 268 at the median, i.e. 32–34 transmissions against V-014's 34–35. Across
two boards the retry count is 33–35 transmissions; `max_retries: 34` stays. Against the model's
expected time to fail (37.0 / 68.4 / 102.6 ms) this board reads 0.87–0.93×, where V-014 read
1.03–1.09×. The V-014 excess was channel load in that run, so the emulator's 0.88–0.90× sits
inside the spread of the two boards and the guest-clock drift in V-014's residual is not
separable from channel load with these data.

---

## V-016 — A/B on two boards: RTT vs payload, board vs emulator

**Setup.** Two ESP32-S3 boards side by side on one PC, RSSI −17 to −30 dBm: firmware A on
`d4:05:92:7b:14:fc` (COM6), firmware B on `d4:05:92:79:90:c8` (COM8), both built with
`-DESPNET_HARDWARE=1`. Firmware A takes a build option `PING_LEN` (default 13, the bare
`ping_msg_t`) and B echoes whatever length arrives. 60 s per size, 600 pings each. The emulator
runs the same sources as `configs/hw_ab.yaml`: one pair per size, pairs 2 km apart so they never
hear each other, 20 s, ~160 pings each.

**Result** (RTT, µs; no ping lost and no reply FAILED on either side; emulator before D-035):

| len | board p50 | board p95 | board mean | emulator p50 | emulator mean | emulator − board |
|---|---|---|---|---|---|---|
| 13 | 3,112 | 5,528 | 3,513 | 5,989 | 6,168 | +2,655 |
| 128 | 4,911 | 6,827 | 5,277 | 5,945 | 6,251 | +974 |
| 250 | 6,920 | 9,418 | 7,370 | 9,545 | 9,521 | +2,151 |

**Reading.** On the boards RTT grows 16.3 µs per byte, two 1 Mbps transmissions of 8 µs per byte,
so the airtime model matches the radio on both legs. The emulator read high by 1.0–2.7 ms,
0.4–1.2 of its 2.2 ms effective quantum in this six-node run (1.8 ms with two nodes, where the
13-byte gap was 1.3 ms). It could not see the 13 → 128 byte step (+83 µs against the boards'
+1.8 ms) because both round trips fit in the same number of quanta; 250 bytes crosses one more
boundary and jumps by a full quantum. So the emulator's latency was right only to within one
quantum, which put timing resolution first; D-035 is the result.

A first 128-byte run had 9 % of RTTs between 10 and 88 ms with no failed reply; a repeat ten
minutes later had none above 15 ms. The repeat is in the table; the first run's tail was other
traffic on channel 1.

---

## D-035 — The barrier's sleep was most of the quantum

**Measurement.** Timing each part of a barrier cycle (two nodes, `slice_us: 250`, 5 s): QMP `cont`
round trip 0.46 ms, the wait between them 1.20 ms, `stop` round trip 0.69 ms; the guests ran
1.87 ms per cycle. The wait was `asyncio.sleep(0.00025)`, which the event loop rounds up to
epoll's 1 ms timeout, and the guests run through all of it.

**Decision.** The barrier waits with a blocking `time.sleep`. Frames that reach the medium during
the wait sit in the socket buffer and are read during the `stop` round trip, or join the next
batch as before (D-029). This supersedes D-029's finding that `slice_us` below the QMP floor does
not matter: the floor was the sleep.

**Result.** Effective quantum 2.0 → 0.85 ms (2 nodes), 2.2 → 1.26 ms (6), 1.16 ms on
`m3_espnow_line`. On `hw_ab` (V-016) the emulator's mean RTT minus the boards' went from
+2.7 / +1.0 / +2.2 ms to +1.1 / +1.4 / +0.9 ms at 13 / 128 / 250 bytes, and RTT now grows
15.6 µs per byte against the boards' 16.3, so the emulator resolves payload size. With only the
13-byte pair (2 nodes, 40 s) the median is 3.73 ms against the boards' 3.11, +0.6 ms.

Smaller slices do not help. With two nodes, `slice_us` 100 and 20 cut the quantum to 0.60 and
0.52 ms (the QMP round trips are now the floor), but the 2-node median RTT at `slice_us: 20` was
4.80 ms against 3.73 at 250. More stop/cont cycles per virtual second cost the guest time
somewhere, perhaps in the shim's TCP over slirp; not measured. Scenarios stay at 250 µs. One run
per setting, so differences under ~0.2 ms are not resolved. 62 tests passed on three consecutive
runs, the wall-clock-sensitive barrier test included.

---

## D-036 — Topology editing: a local page backed by the medium

**Context.** The brief asks for a GUI "for configuring topology" as well as for visualizing runs;
M6's replay page only shows a finished run.

**Decision.** `esp-netsim edit <scenario>` serves one page on 127.0.0.1 (stdlib `http.server`). It
draws the scenario's nodes and AP; nodes can be dragged, added, deleted, and their id, MAC,
channel, firmware and Wi-Fi flag edited, along with the medium and run settings. Every change
posts the scenario to the server, which parses it with the same `parse_scenario` as `run` and
returns each pair's mean RSSI and one-attempt delivery from `MediumEngine.link_quality`, so the
page never carries its own copy of the radio model. Save validates the same way and rewrites the
file; an invalid scenario is refused and the file left alone. The page keeps the scenario as the
raw YAML document, so sections it has no controls for (`traffic`, `port`) pass through.

**Limits.** Comments below the file's leading block are lost on save. The prediction ignores
shadowing, interference and retries (it says so on the page). On WSL the browser reaches the
server through localhost forwarding.

**Checked.** Three HTTP tests (predicted links fall off with distance; a save keeps the header and
other sections; an invalid scenario is refused). In Chrome, driven by Playwright: drag, add node,
change the path-loss exponent, save, and a duplicate MAC shown as an error.

---

## D-037 — Deslop pass after the hardware and editor work

Deleted what had no caller or that no scenario could reach; behavior is unchanged.
- Wire messages carry payloads and LMKs as bytes; the hex round-trip left from the JSON wire
  (D-017, V-005) is gone, with `Message.t`, `FrameDecoder.pending_bytes`, the encode-side size
  check, and `hello()`'s capability arguments that every caller passed the same.
- Path loss and link budget are one `LogDistance(exponent, tx_power_dbm)` (D-006). Shadowing,
  antenna gains, reference distance and frequency were never settable from a scenario.
- The error model keeps DBPSK and the QPSK form; the OFDM M-QAM tables, and
  `ReceptionContext.distance_m`, which nothing read, are gone (D-007). 11 Mbps already resolved
  to the QPSK formula, so no result changes.
- Mock traffic lost `rate_mbps` and per-frame channel (always 1 Mbps on the node's channel).
- The medium's unicast retry loop dropped a duplicate attempt counter and an impossible-None guard;
  the broadcast loop lost an if/else whose branches were identical.
- `esp-netsim info` (fixed text) is gone; `--version` remains.
- `tests/test_review_fixes.py` was split into the files for its subjects; path-loss tests merged.
- Citations: one D-number per file, at the top. Two files cited D-030 (rejected real-time mode)
  for the Wi-Fi bridge, which is D-032.

**Checked.** 62 tests pass. The mock-only scenarios `two_node` and `multi_node_espnow` give
byte-identical `results.json` and `trace.pcapng` before and after; `m2_two_qemu` (26 RTTs, 26 ACKed
replies) and `m4_wifi_gateway` (HTTP 200 from example.com) run with real firmware.

Open, not changed: the shim sends `DEL_PEER` and the medium accepts and ignores it (Appendix B).

---

# PART II — Current-state reference

## Appendix A — Architecture

```
 QEMU esp32s3 (firmware + espnet_shim) ─┐  TCP over open_eth + slirp: HELLO, TX, ADD_PEER
 QEMU esp32s3 ...                       ├──────────────────────────────►  MediumServer
 MockNode (Python stand-in)             ┘  ◄──── RX, TX_STATUS, RUN_SLICE       │
        ▲                                                                      ▼
        │ QMP cont/stop (UNIX socket)                              Simulation (barrier clock)
        └──────────────────────────────────────────────────────────  per slice: run nodes,
                                                                     batch frames, schedule
 Wi-Fi station NIC ──filter-redirector──► wifi_link ──► offer()      deliveries by due time
         ▲                                                  │                  │
         └──── slirp NAT ◄── delivered frames ◄─────────────┘                  ▼
                   │                                            MediumEngine: carrier sense,
                   ▼                                            SINR collisions, retries,
             real Internet                                      path loss, SNR->PER, airtime
                                                                         │
                                         trace.pcapng, results.json, report.html
```

- `orchestrator/runner.py` turns a scenario into processes: one QEMU per firmware node
  (`qemu_launcher.py`: flash image with NVS identity, `-S`, UART to file, QMP), a Wi-Fi bridge per
  station (`wifi_link.py`), a MockNode per remaining node.
- `orchestrator/simulation.py` is the clock master. Per slice: RUN_SLICE to mocks and `cont` to
  QEMU nodes; wait for mock acks and the slice's wall time (blocking sleep, D-035); `stop`;
  advance virtual time by the measured guest time; hand the slice's frames to the engine; queue
  each outcome's deliveries at their computed times; release everything now due (D-029).
- `medium.py` holds positions, channels and peers and decides each frame: carrier-sense deferral,
  per-receiver SINR against overlapping frames with capture, per-attempt retries for unicast
  (`max_retries` 34), ESP-NOW send-callback semantics (D-012, D-031, D-034).
- `channel/`: log-distance path loss (D-006), SNR→PER (D-007), 802.11 DCF airtime (D-008).
- `editor.py`/`editor.html`: the topology editor (D-036).

**Determinism.** One seeded RNG. A mock-only run is a pure function of scenario and seed. A run
with QEMU nodes is not bit-reproducible: guest timing changes the order frames reach the medium.

## Appendix B — Node ⇄ Medium Wire Protocol

TCP to `127.0.0.1:<port>` (the guest reaches it as `10.0.2.2:<port>` through slirp). Every message
is a `uint32` big-endian length and a binary body: `uint8` tag, then fixed big-endian fields; MACs
are 6 raw bytes, payloads `uint16` length + bytes, floats IEEE-754 `float32`. The authoritative
layout is `protocol.py` (`encode_body`/`decode_body`), mirrored by the packed structs in
`espnet_shim.c` and locked by `tests/test_protocol.py`.

| Node → Medium | fields | meaning |
|---|---|---|
| `HELLO` | node, mac, caps | register |
| `ADD_PEER` | node, peer, channel, encrypt, lmk | `esp_now_add_peer` |
| `DEL_PEER` | node, peer | `esp_now_del_peer` (accepted, not modeled) |
| `TX` | node, t, src, dst, channel, rate, seq, data | an ESP-NOW frame offered to the air |
| `SLICE_DONE` | node, t | mock node reached the barrier |

| Medium → Node | fields | meaning |
|---|---|---|
| `WELCOME` | node, slice_us, mac | ack HELLO |
| `TX_STATUS` | node, seq, status, t | the send-callback verdict, at its computed time |
| `RX` | node, t, src, dst, channel, rate, rssi, data | deliver to the receive callback |
| `RUN_SLICE` | until_t | mock node: run to `until_t` |
| `SHUTDOWN` | — | orderly stop |

Guarantees: one TX_STATUS per TX (unicast success iff ACKed within the retry budget; broadcast
success once on the air); RX only for receivers in range, on channel, above the capture threshold;
nothing is released before its computed virtual time. For QEMU nodes the medium ignores `t` and
uses the slice of arrival (D-029).

## Appendix C — Status and next steps

| M | Goal (PDF) | Status | Evidence |
|---|---|---|---|
| M1 | Boot an ESP-IDF image under QEMU, capture UART | done | every firmware run; `docs/m1_qemu_boot.log` (D-020) |
| M2 | Two emulated nodes, controlled delivery and loss | done | `m2_two_qemu`: 20 % constant loss; 5 of 34 pings lost, replies retried and delivered (D-029) |
| M3 | Several nodes, configurable ESP-NOW connectivity, traces | done | `m3_espnow_line`: delivery falls off with distance, carrier sense and hidden terminals (D-031) |
| M4 | Wi-Fi to the real Internet and back | done | `m4_wifi_gateway`: HTTP 200 near, retries at the edge, no DHCP out of range (D-032) |
| M5 | Config file launches all nodes, collects results | done | `esp-netsim run` (D-033) |
| M6 | GUI: nodes, links, packets, console | done as a replay page | `out/<name>/report.html` (D-033) |
| — | GUI topology editing (brief: "configuring topology") | done | `esp-netsim edit` (D-036) |

Scenarios with firmware: `m2_two_qemu`, `m3_espnow_line`, `m4_wifi_gateway`, `example_20_nodes`,
`hw_txprobe`, `hw_ab`; mock-only: `two_node`, `multi_node_espnow`.

Tests: 62 (medium, barrier, wire codec, config, trace, Wi-Fi bridge, editor, runner end to end).
`test_mixed_barrier_qemu_sender_to_mock_receiver` is wall-clock sensitive; it failed about one
run in three around D-034 and passed three consecutive runs after D-035.

Hardware: send-callback timing and retry count on two boards (V-014, V-015); A/B RTT vs payload
on two boards together (V-016). PDR vs distance is not done; it needs the boards moved apart.

Known limits: timing is quantized to an effective quantum of 0.85–1.3 ms of virtual time; RTT
reads ~0.6 ms (2 nodes) to ~1 ms (6 nodes) high against the boards (D-035); delivery, loss and
ordering are exact. Guest clocks drift from host time (D-029). No 802.11 association frames and
no station-to-station traffic through the AP (D-032). Firmware runs are not bit-reproducible.

**Next, in order.**
1. Find why RTT rises as slices shrink (D-035): timestamp the shim's TX and RX path in the guest.
   Then `-icount` or a run-to-next-event barrier for the ~0.5 ms QMP floor.
2. PDR vs distance on boards moved apart (Appendix E).
3. 802.11 association frames and station-to-station traffic through one shared AP.
4. A live (not replay) view of a running experiment.

## Appendix D — Runbook (Linux or WSL2)

```bash
firmware/qemu/setup_esp_idf.sh                   # ESP-IDF v5.4.2, QEMU 9.0.0, libslirp without sudo
. ~/esp/esp-idf/export.sh
for app in espnow_ping espnow_pong wifi_http espnow_txprobe; do idf.py -C firmware/examples/$app build; done
python3 -m venv ~/netsim-venv && ~/netsim-venv/bin/pip install -e ".[qemu,dev]"
export LD_LIBRARY_PATH=$HOME/.local/lib          # if setup fetched libslirp
~/netsim-venv/bin/esp-netsim run configs/m4_wifi_gateway.yaml
```

Gotchas this encodes: the S3 ROM loads the bootloader at 0x0 (the runner uses the build's own
`flasher_args.json`); a build left on the default ESP32 target boot-loops (rejected at scenario
load); QMP must use a UNIX socket (V-007); UART must be written to a Linux path (V-011);
`pkill -f qemu-system-xtensa` also kills the shell that ran it (V-007), so kill by exact name.

## Appendix E — Validation plan

Done, no hardware: V-001 (emergent ~200 m range matches published ESP-NOW LOS range, a
self-consistency check); unit reference points (Friis reference loss, DCF constants, airtime);
M2–M4 behave as the model predicts (loss rate, distance falloff, retries, hidden terminals).

Done, one board at a time (V-014, V-015): firmware A boots unmodified on an ESP32-S3;
send-callback latency for broadcast and for an unanswered unicast, 16–250 bytes, board vs
emulator. This set the retry count to 35 transmissions and made an unanswered unicast radiate.

Done, two boards together (V-016, D-035): RTT vs payload (13, 128, 250 bytes), board vs emulator.
After D-035 the emulator reads 0.6–1.4 ms high and tracks the per-byte slope (15.6 against
16.3 µs per byte).

Next, with the boards apart: PDR vs distance with firmware A and B. Tune `path_loss_exponent`,
`tx_power_dbm`, `noise_figure_db` to the measured curve and report the residual.

Success: PDR curves overlap within measurement variance on one nontrivial multi-node scenario,
and the latency gap is explained by the quantum.
