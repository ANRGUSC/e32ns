# Research Log — ESP32-S3 Cooja-like Network Emulator

> A running, timestamped record of **every** design and implementation decision,
> the alternatives considered, the evidence behind the choice, and the open risks.
> Newest entries are appended at the bottom. Each entry has a stable ID (`D-NNN`)
> so code comments and docs can cite a decision (e.g. `see D-004`).

Legend for each entry:
- **Context** — what problem/question prompted the decision.
- **Options** — alternatives weighed.
- **Decision** — what was chosen.
- **Rationale** — why, with evidence.
- **Consequences / risks** — what this commits us to, what could go wrong.
- **Sources** — primary references (from the attached research brief and PDF, plus first-party docs).

The two source documents this project is built from:
- `../ESP32_Network_Emulator_Project_Description.pdf` — the professor's project brief ("the PDF").
- `../compass_artifact_wf-...text_markdown.md` — the prior deep-dive research brief ("the brief").
Citations of the form *(brief §N)* refer to the numbered Key Findings sections of that brief;
*(PDF)* refers to the project description.

---

## D-000 — Framing: what "develop it" means for this session

**Context.** The task is to *develop* a Cooja-like, multi-node ESP32-S3 network emulator.
The full system (brief §9) spans milestones M1–M6 and depends on the Espressif QEMU fork
plus ESP-IDF, which are Linux-first, heavyweight toolchains.

**Environment probe (this machine):**
- Python 3.11.9 ✓, git 2.50 ✓, Node 24 ✓
- `qemu-system-xtensa` — **not installed**
- `idf.py` (ESP-IDF) — **not installed**
- OS: Windows 11 (win32)

**Decision.** Build the **host-side simulator core as a standalone, fully-tested Python
package** (`esp_netsim`) that is decoupled from QEMU behind an explicit wire protocol
(`docs/PROTOCOL.md`). Provide a pure-Python **virtual node** (`mock_node`) that speaks that
protocol so the *entire* medium + orchestration + tracing stack is runnable and unit-tested
on any machine, including this Windows box and CI, **without QEMU or ESP-IDF**.
The QEMU + ESP-IDF `espnet_shim` path is written as real, review-ready source
(`firmware/`) and documented for integration on a Linux host; it becomes "just another
client" of the same protocol.

**Rationale.**
- The brief's own recommendation (§2 option iii, §9) is a **driver-layer shim + host-side
  Python medium + QMP barrier** — i.e. the medium is inherently a separate host process.
  Decoupling it behind a socket protocol is not a workaround; it is the recommended
  architecture. The mock node simply exercises the *host* end of that same seam.
- Testability: the wireless-medium math (path loss → SNR → PER, airtime, collision) is the
  scientific core and is 100% pure computation. It must be unit-tested against known values;
  it should not require booting an Xtensa VM to test.
- Risk reduction: brief §9 lists three "highest-risk unknowns to spike first." Two of them
  (barrier timing fidelity; shim linkage) require QEMU/IDF, but the medium model and the
  protocol design — the parts that everything else depends on — do not. Build the
  QEMU-independent foundation first and correctly.

**Consequences / risks.** The QEMU/ESP-IDF integration is not executed in this environment;
it is delivered as source + a documented runbook (`docs/QEMU_INTEGRATION.md`). Any claim
about actual on-hardware/on-QEMU timing remains **unvalidated** until run on Linux — this is
called out explicitly wherever it appears, per the honesty requirement.

**Sources.** brief §2 (option iii recommended), §9 (architecture, milestones, risks); PDF
(Scope Principle: "The first version does not need to reproduce the ESP32 radio hardware …
The primary objective is a useful network-emulation abstraction").

---

## D-001 — Language & concurrency model: Python + asyncio

**Context.** Need a language for the orchestrator and the wireless medium.

**Options.** (a) Python+asyncio; (b) C++/Rust for the medium; (c) mixed (Python control, C hot loop).

**Decision.** Python 3.11 with `asyncio` for orchestrator, transport, and medium.

**Rationale.** The brief explicitly recommends Python "matches ANRG's PyWiSim/ncsim
conventions; use `asyncio` for concurrency" (brief §9). ANRG's `PyWiSim` exposes a
`Node`/`on_receive`/`WirelessNetwork` API we mirror for interoperability (brief §10).
The medium's per-slice workload is small (N frames per ~1 ms barrier); Python is fast enough
for tens of nodes (v1 target, brief §10 open question a). If profiling later shows the medium
is the bottleneck at hundreds of nodes, the hot path (path-loss/SINR over N×N pairs) is
isolated behind `channel/` and can be vectorized with NumPy or ported — but we do **not**
prematurely optimize.

**Consequences / risks.** Single-process GIL is fine because the medium is I/O-bound
(socket) and compute-light per slice. Standard-library-only core (no NumPy hard dependency)
keeps install trivial and CI light; NumPy is an optional accelerator, not required.

**Sources.** brief §9 (language/transport/formats), §10 (PyWiSim API shape, scale question).

---

## D-002 — Decouple medium from QEMU behind an explicit wire protocol

**Context.** How do frames get from inside a QEMU-emulated node to the host medium?

**Options (brief §2, five interception points).**
- (i) QEMU Wi-Fi NIC device model — reverse-engineer S3 radio registers. Very high effort;
  only exists for classic ESP32, not S3; "a research project in its own right."
- (ii) Replace closed Wi-Fi blobs (`libpp/libnet80211/libphy`) — brittle, version-locked.
- (iii) **ESP-IDF component overriding `esp_now_*`/`esp_wifi_*`, piping frames to a host
  transport.** Recommended by the brief.
- (iv) `esp_wifi_80211_tx` + promiscuous RX as the seam — still bottoms out in the missing
  radio inside QEMU; useful as the real-hardware validation reference.
- (v) Linker symbol interposition (`--wrap`) — a clean mechanism for (iii).

**Decision.** Adopt **(iii)+(v)**: a host-side medium process with a stable, transport-agnostic
**wire protocol** (`docs/PROTOCOL.md`); inside the node an `espnet_shim` provides the
`esp_now_*` public symbols and serializes frames to the transport. The protocol is the single
contract; QEMU nodes and the pure-Python `mock_node` are interchangeable clients.

**Rationale.** Preserves full firmware fidelity (app + FreeRTOS + lwIP + the ESP-NOW API
contract) which is "the whole point of the project" (brief §6 verdict; PDF Core Idea), while
avoiding the multi-month radio-register reverse-engineering of (i)/(ii). Defining an explicit
protocol (rather than an ad-hoc pipe) is what makes the medium independently testable (D-000)
and lets us swap transports (TCP socket vs `open_eth` vs UART) without touching medium logic
(brief §9 risk 2: "cleanest transport … spike both").

**Consequences / risks.** Sacrifices real MAC/PHY behavior — explicitly deprioritized for v1
by the professor (PDF Scope Principle; brief §2 iii). We reproduce MAC *semantics* (ACK,
retry, collision, airtime) in the medium instead of in emulated silicon.

**Sources.** brief §2 (all five options), §6 (fidelity is the point), §9; PDF (Core Idea, Scope).

---

## D-003 — Transport for v1: length-prefixed TCP over loopback

**Context.** The protocol needs a concrete byte transport between each node and the medium.
The brief floats `open_eth` socket netdev, a dedicated UART/USB-serial-JTAG channel, and
UNIX domain sockets (brief §9, §4, caveats).

**Options.** (a) TCP loopback, length-prefixed; (b) UNIX domain sockets; (c) QEMU
`-netdev socket` UDP multicast; (d) UART/pty channel.

**Decision.** For v1, **TCP over 127.0.0.1 with a 4-byte big-endian length prefix per
message**, one connection per node to a central medium server. UNIX sockets and the QEMU
`open_eth`/UART transports are kept as future `transport/` backends behind the same codec.

**Rationale.**
- Portability: this repo is developed on **Windows** (no historical AF_UNIX guarantee; QEMU
  `-netdev socket` TCP mode also has documented reconnect fragility — brief caveat).
  TCP loopback works identically on Windows/Linux/macOS, so the pure-Python pipeline runs
  everywhere and in CI.
- The brief flags QEMU `-netdev socket` TCP reconnect fragility ("rebooting the listen side
  breaks the connect side") and recommends UDP/dgram or a dedicated control channel for
  robustness. Our medium is the stable *server*; nodes are clients that reconnect — explicit
  length-prefix framing avoids the fragility by making the medium the long-lived listener and
  giving each message a definite boundary.
- Length-prefixing (not delimiter-based) is robust to arbitrary binary payloads (802.11
  frames contain any byte).

**Consequences / risks.** TCP adds per-node head-of-line ordering (fine — a node's own frames
are ordered anyway) and a small syscall cost per frame; negligible at v1 scale. When we wire
real QEMU we re-evaluate `open_eth` vs UART (brief §9 risk 2) but the medium code is untouched
because it only sees the codec.

**Sources.** brief §9 (transport options), caveats (`-netdev socket` reconnect fragility).

---

## D-004 — Shared virtual time via a SliceTime-style barrier (not -icount)

**Context.** N independent QEMU processes each have their own clock. Cooja's key property is a
single deterministic simulation clock across all motes (brief §4). We recreate that across
processes.

**Options.**
- `-icount` deterministic instruction clock: but "does not synchronize multiple QEMU
  processes," and is "unsupported/unused on the Espressif Xtensa S3 fork" (brief §1, §4).
- External barrier gating via QMP `stop`/`cont` in fixed slices (SliceTime / Renode / SimBricks
  style): pause all nodes at slice boundaries, exchange queued packets while stopped, `cont`
  all (brief §4, §6).

**Decision.** A **clock master** that advances virtual time in fixed **slices** (default
1 ms virtual, tunable — brief §9), gating each node at the barrier. All queued frames for a
slice are processed by the medium while every node is paused, then all nodes are released.
For QEMU nodes the gate is QMP `stop`/`cont`; for `mock_node` the gate is an asyncio barrier.
The gating mechanism is abstracted so both node kinds share one clock master.

**Rationale.** Direct architectural template from SliceTime (NSDI 2011), TimeKeeper (TOMACS
2018), SimBricks (SIGCOMM 2022); mirrors Renode's time-quantum barrier — "the strongest reason
to study Renode" (brief §6). It guarantees perceived virtual-time skew between any two nodes
never exceeds one quantum. Sidesteps both `-icount` problems (no cross-process sync;
unsupported on the fork) and avoids the EmuLink trap where an external emulator on the wall
clock made Cooja non-deterministic (brief §4).

**Consequences / risks.** brief §9's **highest-risk unknown #1**: whether a 1 ms QMP barrier
holds ESP-NOW latency (~1–2 ms real MAC) tightly without wrecking throughput. The clock master
has a *configurable slice* and a *deterministic offline mode* (run-to-barrier without
real-time pacing) as the documented fallback (brief thresholds). This session validates the
barrier only against `mock_node` (QMP path is source-only).

**Sources.** brief §1, §4 (icount limits, QMP gating, EmuLink trap), §6 (Renode time
framework), §9 (slice default, risk #1), thresholds (offline fallback).

---

## D-005 — Virtual-time unit and representation: integer microseconds

**Context.** The medium schedules deliveries at "the correct virtual time"; ESP-NOW MAC
latency is ~1 ms and airtime is computed in microseconds (brief §3, §5: SIFS 10 µs, slot
20 µs, DIFS 50 µs).

**Decision.** Represent virtual time as **integer microseconds** (`int`), monotonic from 0.

**Rationale.** Integer time avoids floating-point drift accumulation across millions of slices
(determinism/reproducibility is a core goal — PDF Longer-Term Vision). Microsecond resolution
represents SIFS/slot/airtime exactly and matches the real-hardware latency figures we validate
against (1059 µs / 1869 µs, brief §3). Nanoseconds would be overkill; µs in int64 is safe for
~292k years.

**Consequences.** Model outputs (airtime, propagation delay) are rounded to whole µs at the
medium boundary; sub-µs precision is kept inside float computations and quantized only when
scheduling an event.

**Sources.** brief §3 (latency figures), §5 (DCF timing constants).

---

## D-006 — Path loss: log-distance with optional log-normal shadowing

**Context.** The medium must map node geometry → received power. brief §5 prescribes ns-3's
`LogDistancePropagationLossModel`.

**Decision.** Implement `LogDistancePropagationLoss`:
`PL(d) = PL(d0) + 10·n·log10(d/d0) + X_sigma`, with defaults **n = 3.0** and
**PL(d0=1m) = 40.046 dB** (Friis at 2.4 GHz), and optional Gaussian shadowing `X_sigma`
(σ dB, default 0 → deterministic). `Prx = Ptx + Gtx + Grx − PL(d)`.

**Rationale.** These are the exact ns-3 defaults the brief cites: exponent 3.0 and
ReferenceLoss 40.0459 dB at 1 m for 2.4 GHz 802.11b (brief §5). I recomputed the Friis
reference independently: `20·log10(4π·d0·f/c)` with f=2.4 GHz, d0=1 m = **40.046 dB** — matches
ns-3's 40.0459, so the constant is verified, not copied blindly. Shadowing off by default keeps
runs reproducible (a core goal); turning it on requires a seeded RNG (D-011) so runs stay
repeatable. `ThreeLogDistance`/`Nakagami` fast-fading (brief §5) are noted as future subclasses
behind the same `PropagationLoss` interface.

**Consequences / risks.** Real indoor/outdoor exponents vary (2–4); we expose `n`, `d0`,
`pl_d0`, `sigma`, and antenna gains as config so a user can fit to the ANRG PRR datasets
(brief §10, anrg.usc.edu/downloads) during validation.

**Sources.** brief §5 (ns-3 LogDistance defaults, exponent 3.0, ReferenceLoss 40.0459),
§10 (ANRG PRR datasets for fitting).

---

## D-007 — Error model: pluggable, with three v1 implementations

**Context.** brief §5 prescribes a YANS-style SNR→PER curve; brief §2/§9 (M2) wants a simple
constant-loss model first; brief §10 raises the gfge Gilbert-Elliott closed form and a "simpler
Gilbert-Elliott link model" as alternatives to the full YANS curve.

**Decision.** Define an `ErrorRateModel` interface `per(ctx) -> float` and ship three:
1. `ConstantErrorModel(per)` — fixed packet-error-rate. The M2 controlled-loss model
   (Cooja UDGM constant-loss analog).
2. `RangeThresholdErrorModel(tx_range, success_ratio)` — Cooja **UDGM** style: deliver with
   `success_ratio` inside `tx_range`, drop beyond. Geometry-driven, no SNR.
3. `SnrPerErrorModel` — the **physical** model: uses path loss → SNR → per-modulation BER →
   `PER = 1 − (1 − BER)^(8·nbytes)`. DBPSK for 1 Mbps DSSS (ESP-NOW default), DQPSK for 2 Mbps,
   Gray-coded M-QAM for OFDM rates.

**Rationale.** Matches the brief's *staged* fidelity: M2 uses (1), M3 uses (3); (2) reproduces
Cooja UDGM exactly for users who want the familiar knob (brief §4 UDGM). Making the model an
interface honors brief §10(d): "affects whether the YANS SNR-to-PER curve or a simpler
Gilbert-Elliott link model is used" — we can drop a `gfge`/Gilbert-Elliott model in later
without touching the medium. **Honesty note:** I cannot bit-match ns-3 `YansErrorRateModel`
offline (its source isn't in this environment). I implement the *standard digital-comms BER
formulas that YANS itself is built on* (DBPSK: `0.5·e^(−Eb/N0)`; coherent BPSK/QAM: Q-function
forms) and validate **invariants** (PER∈[0,1], monotonically decreasing in SNR, →0 at high SNR,
→~1 at very low SNR) plus a few hand-computed reference points — not bit-exactness. This is
labeled clearly in code and tests. Bit-exact YANS parity is a future task requiring the ns-3
source (or GSL) to cross-check.

**Consequences / risks.** Eb/N0 = SNR·(B/R) where B = noise bandwidth, R = bit rate — a standard
link-budget identity; the absolute PER curve depends on assumed noise figure and channel
bandwidth, which are config knobs, so the model is *tunable* to match measured PRR rather than
asserted as ground truth.

**Sources.** brief §5 (YANS model, product-of-chunks), §2/§9 (constant-loss first), §4 (UDGM
success ratios), §10(d) (YANS vs Gilbert-Elliott choice), §10 (gfge closed form).

---

## D-008 — Airtime: 802.11 DCF closed-form (DIFS/SIFS/backoff/PLCP)

**Context.** brief §5 specifies DCF timing and an airtime formula; brief §3 gives ESP-NOW
default rate 1 Mbps and real one-way latencies (1059 µs @1 B, 1869 µs @100 B) to reproduce.

**Decision.** Compute airtime as
`DIFS + backoff + PLCP_preamble+header + (MAC_hdr+payload+FCS)·8/rate + SIFS + ACK`
with DSSS constants slot = 20 µs, SIFS = 10 µs, DIFS = SIFS + 2·slot = 50 µs, long PLCP
preamble+header = 192 µs (brief §5). Backoff is a seeded uniform draw in `[0, CW·slot]`.

**Rationale.** These are exactly the brief's DCF constants (brief §5). Reproducing a small base
latency (~1 ms MAC) plus a payload-dependent term is precisely what brief §3 asks of the model.
Sanity check against brief §3: a 1-byte ESP-NOW action frame at 1 Mbps ≈ 192 µs PLCP +
~(24+1+4)·8 µs + 50 + 10 + ACK ≈ order 10³ µs — same order as the measured 1059 µs, so the
formula is calibrated to the right regime (full validation deferred to hardware, D-000).

**Consequences / risks.** ESP-NOW rides vendor-specific action frames; exact header sizes differ
from a data frame. We expose header/preamble/rate as parameters so airtime can be tuned to the
WONS-2025 numbers during validation instead of hard-coded.

**Sources.** brief §5 (DCF constants, airtime formula), §3 (ESP-NOW 1 Mbps, measured latency).

---

## D-009 — Collision & capture: per-frame SINR against summed interference

**Context.** brief §5: "ns-3 computes SINR per chunk against summed interference power, which is
the more faithful approach" than Cooja UDGM's binary in-range collision flag; capture effect
delivers the stronger frame when SINR exceeds a threshold (brief §3, §5).

**Decision.** For each candidate reception, compute SINR = Prx / (N + Σ Pinterferers) over the
overlap window, on the same channel; deliver iff SINR ≥ capture threshold *and* the error model
(fed that SINR) says the packet survives. Same-channel-only for v1 (matches ESP-NOW's
shared-channel peer constraint, brief §5).

**Rationale.** The brief calls the SINR approach "more faithful" and notes Cooja UDGM's binary
collision as the coarser alternative (brief §5). Capture effect is real ESP-NOW/Wi-Fi behavior
(brief §3). Same-channel-only is explicitly "sufficient for v1" per brief §5 and matches the
ESP-NOW constraint that all peers share a channel (brief §3). Adjacent-channel spectral overlap
masks are noted as future work (brief §5).

**Consequences / risks.** Requires the medium to know the time-overlap of concurrent
transmissions → the barrier/virtual-clock must timestamp TX start and airtime (ties to D-004,
D-008). At v1 scale an O(N²) interferer sum per frame is fine (D-001).

**Sources.** brief §5 (SINR vs UDGM, capture, channel overlap), §3 (ESP-NOW shared channel,
capture/ACK).

---

## D-011 — Determinism: one seeded RNG, threaded through the medium

**Context.** Shadowing (D-006), per-packet loss draws (D-007), and DCF backoff (D-008) all need
randomness, but reproducibility is a stated project goal (PDF Longer-Term Vision; the whole
point vs. real hardware).

**Decision.** A single `random.Random(seed)` owned by the medium, seeded from config
(default fixed). Every stochastic draw goes through it. No module-level `random.*` calls.

**Rationale.** "Controllability and repeatability of a network simulator" (PDF) requires that a
given (config, seed) reproduces byte-identical traces. A single threaded RNG makes the whole run
a pure function of (config, seed, node I/O order); the barrier (D-004) fixes the I/O order.

**Consequences / risks.** Parallelizing the medium later would require per-stream RNGs
(splittable/counter-based) to preserve determinism; noted for the hundreds-of-nodes case.

**Sources.** PDF (repeatability); brief §9 (reproducible runs).

---

## D-012 — ESP-NOW semantics the medium reproduces (the core behavior)

**Context.** brief §3 names the send-callback result as "the single most important behavior to
reproduce," and enumerates the exact ESP-NOW rules.

**Decision.** The `MediumEngine` reproduces, in order:
1. **Send-callback verdict** per TX (→ `TX_STATUS`): broadcast ⇒ `success` once transmitted
   (unacknowledged); unicast ⇒ `success` iff delivered+ACKed within the retry budget, else
   `fail`. Documented `fail` causes wired explicitly: **peer/dst does not exist**, **channel
   mismatch**, **lost on air** (brief §3).
2. **RX dispatch:** broadcast → every in-range node that passes SINR/PER; unicast → only the
   addressed peer node (encrypted unicast additionally requires the peer be added with a
   matching LMK — brief §3). Broadcast needs no `add_peer`.
3. **Retries:** unicast reattempts up to `max_retries` (default modeling ~ a handful), each
   redrawing the loss and adding airtime; success if any attempt is delivered.
4. **Payload cap:** default `ESP_NOW_MAX_DATA_LEN = 250` (v1.0); `max_data_len` is config so
   v2.0 (1470, needs IDF ≥5.4.2) can be selected later (brief §3, §10c).
5. **Rate:** default 1 Mbps (brief §3).

**Rationale.** These are the precise rules in brief §3; getting the callback semantics right is
what makes an ESP-NOW app behave the same under emulation as on hardware (the app-level
ACK/retransmit pattern the ESP-NOW docs recommend depends on this callback, brief §3).

**Consequences / risks.** CCMP encryption is modeled at the *policy* level (does the receiver
have the peer+LMK?) not cryptographically — v1 does not actually AES-encrypt payloads; the
frame bytes pass through. Flagged as a fidelity gap; real CCMP is future work and irrelevant to
delivery timing/PER.

**Sources.** brief §3 (all ESP-NOW rules), §10c (v1.0 vs v2.0 payload).

---

## D-013 — Collision model granularity: overlapping-window SINR within the medium

**Context.** D-009 chose SINR-vs-summed-interference. Implementation must define *which*
transmissions interfere.

**Decision.** The engine tracks in-flight transmissions with their `[start, start+airtime]`
window and channel. A reception's SINR sums the received power of every *other* transmission
whose window overlaps the desired frame's window on the *same channel*. Frames submitted
together in a barrier slice are evaluated against each other symmetrically
(`process_batch`), and against still-in-flight frames from prior slices.

**Rationale.** Directly realizes the "SINR per chunk against summed interference power"
approach the brief calls more faithful than UDGM's binary collision flag (brief §5). Same-
channel-only matches ESP-NOW's shared-channel constraint and the brief's v1 sufficiency note
(brief §3, §5). Propagation delay (~3.3 ns/m) is far below the µs grid so reception window ≈
transmission window; we ignore the sub-µs shift for overlap tests (documented approximation).

**Consequences / risks.** Whole-frame single-chunk (not per-field sub-chunk) SINR; a frame that
overlaps another for only part of its duration is treated as fully interfered for v1
(conservative). Per-symbol/per-chunk interference integration is future work (matches YANS
refinement noted in D-007).

**Sources.** brief §5 (SINR vs UDGM, same-channel v1), §3 (shared channel).

---

## D-014 — Barrier dispatch timing: deliveries computed while paused, sent at boundary

**Context.** In the barrier loop (D-004), a frame transmitted mid-slice has a delivery time
(`start + airtime + prop`) that may land inside the same slice or a later one. When should the
medium hand RX/TX_STATUS back to nodes?

**Decision (v1).** Collect all frames a slice produces, process them as one interfering batch
*after* every node has hit the barrier (SLICE_DONE), then dispatch TX_STATUS/RX. Nodes consume
those at the next release. So a frame sent in slice *k* is acted on by receivers at the start of
slice *k+1* at the latest.

**Rationale.** This is the literal SliceTime/Renode discipline: "a synchronization phase during
which no node executes where all inter-node communication is processed" (brief §6). It keeps the
medium a pure function of the slice's frame set and guarantees causal ordering (RX never precedes
its TX). It bounds delivery latency error to ≤ one slice — exactly the quantum guarantee (D-004).

**Consequences / risks.** With a 1 ms slice, sub-millisecond ESP-NOW MAC latencies are quantized
to the slice grid; this is brief §9 risk #1 (does the barrier hold timing tightly?). The slice is
configurable, and a finer slice tightens timing at a throughput cost — the exact trade to spike on
real QEMU. For now the model *reports* the physically-computed `airtime`-based delivery time in the
RX/status `t` field even though dispatch is quantized, so traces carry the true latency (matching
Wokwi's "stamp traces with simulation-clock time," brief §7).

**Sources.** brief §6 (Renode sync phase), §7 (simulation-clock timestamps), §9 (risk #1).

---

## D-015 — MockNode as a first-class virtual node (test/demo without QEMU)

**Context.** The QEMU + ESP-IDF path cannot run in this environment (D-000), yet the barrier,
transport, and ESP-NOW semantics must be demonstrably correct.

**Decision.** Ship `nodes/mock_node.MockNode`: a pure-Python client that implements the *node*
half of the protocol — HELLO/WELCOME, RUN_SLICE→emit scheduled TX→SLICE_DONE, and RX/TX_STATUS
recording. It mirrors exactly what the ESP-IDF `espnet_shim` does (intercept `esp_now_send`,
serialize, translate callbacks back).

**Rationale.** Makes M2/M3 behavior CI-testable today and gives users a fast, QEMU-free way to
develop topologies and the medium model. Because it is *the same protocol*, a scenario validated
with mock nodes will behave identically when a node is swapped for QEMU+shim (the medium can't
tell them apart) — this is the payoff of D-002's explicit-protocol decision.

**Consequences / risks.** MockNode runs application logic in Python, not the real ESP-IDF app, so
it does **not** validate firmware fidelity — that is exactly what the QEMU path adds later. The
two are complementary: MockNode validates the *medium/orchestration*; QEMU validates the *firmware*.

**Sources.** brief §2/§6 (firmware fidelity is QEMU's job); D-000, D-002.

---

## D-016 — Trace format: pcapng + radiotap, timestamps in simulation-clock µs

**Context.** brief §9 prescribes "pcapng with radiotap headers (per-packet RSSI, channel, rate,
plus a comment field recording the loss reason and computed SNR)"; brief §7: stamp traces with
*simulation-clock* time, not wall-clock.

**Decision.** Write **pcapng** (not classic pcap) with `LINKTYPE_IEEE802_11_RADIOTAP` (127).
Each packet = a radiotap header (Flags, Rate, Channel freq, dBm antenna signal) + a synthesized
802.11 vendor-specific **action** frame (Espressif OUI `18:fe:34`) wrapping the ESP-NOW payload.
Timestamp resolution is microseconds (`if_tsresol=6`), values are the **virtual** delivery time
(D-014). Each Enhanced Packet Block carries an `opt_comment` with the medium's internals:
loss reason, SNR/SINR dB, PER, attempts.

**Rationale.** Exactly the brief's format choice: pcapng's per-packet comments let us annotate
every frame with medium-model internals while staying natively Wireshark-readable (brief §9). We
also capture *dropped* frames (with reason) not just delivered ones — invaluable for debugging a
link model, and something a plain on-air capture can't do. Simulation-clock timestamps keep
timing analysis correct even when the sim runs slower/faster than real time (Wokwi's lesson,
brief §7). Synthesizing the vendor action frame (rather than dumping raw payload) makes Wireshark
dissect src/dst/BSSID and recognize the Espressif OUI.

**Consequences / risks.** The synthesized 802.11 frame is a faithful-*shape* representation, not a
byte-exact ESP-NOW element (the exact vendor element sub-format is future fidelity work); this is
labeled in code. Writing pcapng by hand (no third-party lib) keeps dependencies minimal and is
straightforward given the block format.

**Sources.** brief §9 (pcapng+radiotap+comments), §7 (simulation-clock timestamps, Espressif
OUI/MAC conventions), §3 (ESP-NOW as vendor action frames).

---

## V-001 — First sanity validation: emergent range matches ESP-NOW field data

**Context.** After wiring the physical `snr_per` model, I ran a distance sweep (2 nodes, one
frame per distance, 50 trials each; Ptx=20 dBm, exponent 3.0, NF 7 dB, capture 4 dB, 50-byte
payload @ 1 Mbps).

**Result (this environment):**

| dist (m) | SNR (dB) | PER        | delivered/50 |
|----------|----------|------------|--------------|
| 10       | 43.5     | 0          | 50 |
| 50       | 22.5     | 0          | 50 |
| 100      | 13.5     | ~1e-212    | 50 |
| 150      | 8.2      | ~9e-62     | 50 |
| 200      | 4.5      | ~4e-25     | 50 |
| 250      | 1.6      | 1.0 (below capture) | 0 |
| 300      | -0.8     | 1.0        | 0 |

**Interpretation.** The model yields an effective line-of-sight range of ~200–220 m before the
link falls below the 4 dB capture threshold. This **matches the brief's reported ESP-NOW LOS
range of 190 m / 220 m / up to 250 m** (brief §3) — an independent, order-of-magnitude-plus
sanity check that the link budget + path-loss + threshold are jointly calibrated to the right
regime. The transition is currently a sharp cliff (200 m perfect → 250 m dead) because shadowing
is off (σ=0, deterministic) and the capture gate is hard; enabling `shadowing_sigma_db` (D-006)
smooths it into the heavy-tailed PDR-vs-distance curve the WONS-2025 study reports (brief §3).

**Caveat (honesty, per D-000).** This is a *model self-consistency* check against published
range figures, **not** a hardware validation. The centerpiece validation the brief recommends
— ESP-NOW latency and PDR on virtual nodes vs. real ESP32-S3 boards (brief §9 rec 3) — requires
the QEMU path on Linux and real hardware, and remains future work (see docs/QEMU_INTEGRATION.md,
docs/VALIDATION.md).

**Sources.** brief §3 (ESP-NOW range 190–250 m; heavy-tailed PDR), §9 (hardware-validation goal).

---

## D-017 — ESP-IDF shim: override `esp_now_*`, JSON-over-transport, callback bridge

**Context.** The QEMU node needs a component that makes an unmodified ESP-NOW app talk to the
host medium instead of a real radio (brief §2 option iii/v).

**Decision.** Ship `firmware/components/espnet_shim`: a component that **provides the public
`esp_now_*` symbols** (`esp_now_init`, `esp_now_send`, `esp_now_add_peer`, register recv/send
cb, `esp_now_set_pmk`, …). `esp_now_send` serializes a `TX` message (same JSON wire protocol,
D-002/PROTOCOL.md) to a transport FD; a background task reads `TX_STATUS`→ invokes the app's
send-cb, and `RX`→ invokes the app's recv-cb. Linkage uses component-provided symbols (option v);
a `--wrap` variant is documented as the fallback if the app also links real `esp_now`.

**Rationale.** This is precisely brief §2's recommended path (iii)+(v): preserve the app +
FreeRTOS + lwIP + the ESP-NOW *API contract* while replacing only the radio bottom. Emitting the
exact same JSON protocol the mock node uses means the medium is unchanged (D-015 payoff). Written
in portable C99 with a tiny hand-rolled JSON encoder so it needs no cJSON (though cJSON is fine),
matching the "no third-party JSON lib required" note in PROTOCOL.md.

**Consequences / risks.** brief §9 risk #3: "whether the shim can be linked cleanly over IDF
without modifying the application (test `--wrap` and component link ordering)." We provide both
mechanisms and a spike checklist; this cannot be compiled/verified in the current Windows
environment (no ESP-IDF) and is flagged as source-only pending a Linux+IDF spike (D-000).
The transport FD choice on real QEMU (open_eth socket vs UART vs USB-serial-JTAG) is brief §9
risk #2 and is abstracted behind `espnet_transport_*` so it can be swapped after the spike.

**Sources.** brief §2 (iii/v), §3 (esp_now API surface), §9 (risks #2/#3, milestones M1–M3).

---

## D-018 — Code-review pass: three correctness fixes + modeling clarification

**Context.** After the mock path was green (54 tests), an independent `code-reviewer`
sub-agent audited the Python core for correctness/concurrency/physics bugs (per the user's
global agent-orchestration rules). It confirmed determinism, SINR algebra, retry off-by-one,
FrameDecoder framing, and the intra-slice barrier reset were clean, and found three real
issues + two cosmetics. All were fixed and locked with regression tests
(`tests/test_review_fixes.py`).

**Fixes applied:**
1. **BUG-1 (HIGH) — phantom interference.** `process_batch` registered *every* frame as
   in-flight, including frames rejected before transmission (peer-not-exist, channel-mismatch,
   wifi-not-started, encrypt-reject). Those emit no RF and were poisoning later frames' SINR.
   Fixed: only frames that actually radiated (`attempts > 0`) are registered as interferers
   (`medium.py`).
   *Modeling clarification (v1):* API/addressing-rejected frames are treated as "did not reach
   the air." Physically, a channel-mismatch or absent-destination frame does radiate on the
   sender's channel and could interfere; modeling that requires separating *radiation* from
   *reception verdict*. Deferred as a fidelity refinement; the simplification is predictable
   and documented. Also note a node cannot physically transmit two frames at the same instant —
   the orchestrator serializes a node's own TX with monotonic timestamps, so same-node
   self-interference does not arise in practice.
2. **BUG-2 (MEDIUM) — stale barrier release.** `SLICE_DONE` was counted without checking which
   slice it belonged to; a late ack from slice *k−1* (possible once QEMU nodes add latency)
   could release slice *k*'s barrier early. Fixed: the handler now only counts a `SLICE_DONE`
   whose `t` equals the in-progress slice's `until_us` (`simulation.py`).
3. **BUG-3 (MEDIUM) — RX timestamp late by SIFS+ACK.** A unicast RX was timestamped at the end
   of the *acknowledged* airtime (after the ACK), ~314 µs late at 1 Mbps. Fixed: the data frame
   lands `ack_tail = SIFS + ACK` before the airtime ends; the sender's send-callback still fires
   at full airtime. Added `AirtimeModel.ack_tail_us()` and corrected `_evaluate_unicast`
   (`medium.py`). This makes trace timestamps (D-016) physically correct.
4. Cosmetics: relabelled a weak-link drop `below_sensitivity` (vs `collision`) so pcapng
   comments don't imply a collision that didn't happen; removed a dead `prop` variable.

**Rationale.** These are correctness improvements to the scientific core and the barrier — the
exact places where silent-but-deterministic wrong answers would undermine the tool's credibility
(the reviewer's key point: BUG-1 gives wrong SINR silently). Fixing them now, with regression
tests, before the QEMU integration where BUG-2/BUG-3 would first bite.

**Sources.** Internal code review (sub-agent), cross-checked against D-008/D-009/D-012/D-013.

---

## D-019 — Course correction: reverse D-000, do the emulator, spike risks first

**Context.** A critical review (advisor-style) landed after the mock path was green. The core
charge is correct and I accept it: **the project built a simulator and skipped the emulator.**
M1 — "boot an ESP32-S3 image under QEMU and capture UART" — is the first and easiest milestone
and the one thing I did *not* do. I justified this in D-000 with "qemu-system-xtensa not
installed, Windows," and treated a **missing binary as an environmental constraint** instead of
a solvable setup step. That was wrong on the facts:

- This machine has **WSL2 Ubuntu 24.04**, working, with internet to `dl.espressif.com`
  (verified). 927 GB free, 20 cores. ESP-IDF + Espressif QEMU is a standard install here.
- So the entire session's decoupling-first architecture, while defensible on its own merits,
  was adopted for the **wrong reason** and in the **wrong order**.

**The inverted risk order (the deeper mistake).** The brief (§9) says spike the three
highest-risk unknowns *first*: (1) QMP barrier timing fidelity, (2) frame transport out of QEMU,
(3) shim linkage over IDF. I built **100% of the zero-risk part** (path-loss math — textbook,
never in doubt) and **0% of the risky part**. 54 passing tests currently rest on three
unverified assumptions; if the shim doesn't link or the barrier can't hold sub-ms timing, parts
of the Python get rewritten.

**Accepted specific corrections:**
- M2/M3/M5 must **not** be presented as "done." They are: *the medium/orchestration/tracing/
  config layers, validated against a software stand-in (`MockNode`)*. No ESP-IDF firmware has
  executed. "Done on the mock path" was over-claiming; milestones mean nothing without firmware.
- `espnet_shim.c` has **never been compiled** — it is risk #3 verbatim, source-only.
- **V-001 is a units check, not validation** (the inputs determine the ~200 m output). The log's
  own caveat said as much; keeping V-001 but re-labelling it explicitly as self-consistency.
- **Design smell accepted:** JSON-over-the-wire on a per-frame hot path with µs timing
  sensitivity, hand-encoded in C on an emulated Xtensa core, adds cost and a variable to the
  timing spike. Plan: switch the transport to **length-prefixed binary** before the transport
  spike (protocol.py already frames by length; only the body encoding changes — the JSON debug
  form stays available behind a flag).

**Revised plan (this is now the priority order, replacing the milestone table's optimism):**
1. **M1 for real:** WSL2 + ESP-IDF v5.4.2 + Espressif QEMU; boot `hello_world` on `esp32s3`,
   capture UART. (Install running in background as this is written.)
2. **Spike #3 (shim linkage):** get `espnet_shim` to compile and link over IDF; confirm the app
   calls our `esp_now_send`, not the real one (`--wrap` vs component provision). Most likely to
   break.
3. **Spike #2 (transport):** get one message out of QEMU to a host listener; measure per-frame
   overhead; switch to binary framing.
4. **Reconnect the Python medium** and rerun `two_node.yaml` with **one real QEMU node**.
5. Then M4, hardware validation, M6.

**What survives the correction (not thrown away):** the medium genuinely *is* a separate host
process, so the protocol decoupling remains a defensible architecture — but the motivation is now
"the medium is out-of-process by nature," not "QEMU won't run here." The self-review fixes (D-018)
and the config/trace/CLI infrastructure had to be built eventually. RESEARCH_LOG.md itself is the
artifact the advisor wanted. The mock path is retained as a **CI/unit harness for the medium**,
not as a milestone claim.

**Sources.** Advisor review (2026-08-31); brief §9 (risk-first ordering), PDF (M1 first).

---

## D-020 / V-002 — M1 achieved: real ESP-IDF firmware booted on emulated ESP32-S3

**Context.** Acting on D-019, I set up the real toolchain instead of architecting around its
absence, and ran M1 — the milestone I had wrongly skipped.

**Result (reproduced on this machine).** ESP-IDF **v5.4.2** `hello_world`, built for `esp32s3`,
booted under the **Espressif QEMU fork 9.0.0** in WSL2, producing genuine UART:
```
I (139) main_task: Calling app_main()
Hello world!
This is esp32s3 chip with 2 CPU core(s), WiFi/BLE, silicon revision v0.3, 2MB external flash
Minimum free heap size: 390052 bytes
Restarting in 10 seconds...
```
Full boot log: `docs/m1_qemu_boot.log` (real 2nd-stage bootloader, partition table, heap init,
dual-core start). Reproducible via `firmware/qemu/setup_esp_idf.sh` + `firmware/qemu/run_qemu.sh`.

**Non-obvious things learned (each cost a real iteration — documented so nobody re-hits them):**
1. **WSL tears down fire-and-forget background jobs.** `wsl.exe -e bash -lc '... &'` with `nohup`
   still died when the launching process exited (the distro shuts down). Fix: run long jobs as a
   *harness-tracked* process (or inside a persistent shell) so `wsl.exe` stays alive.
2. **QEMU needs `libslirp.so.0`, and `sudo` was unavailable.** Got it without root via
   `apt-get download libslirp0` (no sudo) + `dpkg -x` into `~/.local/lib` + `LD_LIBRARY_PATH`.
   Bonus: libslirp is exactly the user-mode-NAT lib M4's `open_eth` path needs anyway.
3. **ESP-IDF doesn't install cmake/ninja on Linux.** Added both to the IDF python env with
   `pip install cmake ninja` — no sudo.
4. **`idf.py qemu`'s own tool-check rejected the QEMU** (libslirp verification quirk); running
   `qemu-system-xtensa` directly with `LD_LIBRARY_PATH` set works fine, so the launcher calls
   QEMU directly (this matches `orchestrator/qemu_launcher.py`'s design).
5. **ESP32-S3 boots the 2nd-stage bootloader from flash offset `0x0`**, not `0x1000` (the ESP32/
   -S2 convention). A `merge_bin` at `0x1000` gives the ROM `invalid header: 0xffffffff`.
6. **A silently-failed `set-target` is a trap.** My first `idf.py set-target esp32s3` ran before
   cmake was installed and failed; the project silently stayed on the default `esp32` target, so
   QEMU rejected the image with `Invalid chip id. Expected 9 read 0`. Always confirm
   `CONFIG_IDF_TARGET` after set-target.

**Consequences.** This overturns D-000's premise entirely: the emulator path is not just viable
here, it *runs*. The firmware-fidelity claim (brief §6, PDF Core Idea) is now demonstrated at the
boot level. Next: the three risk spikes (shim linkage, transport, barrier) now that a node boots.

**Sources.** ESP-IDF v5.4.2 docs (QEMU, set-target); Espressif QEMU fork 9.0.0; this machine.

---

## D-021 / V-003 — Spike #3 RESOLVED: shim linkage requires `--wrap` (empirical)

**Context.** brief §9 risk #3 (the "most likely to break", per the review): can the shim intercept
`esp_now_*` over IDF without editing the app? I tested it on real firmware in QEMU instead of
assuming.

**Findings (in order, each run on the emulated esp32s3):**
1. **Naive component symbol provision FAILS.** With the shim providing strong `esp_now_*`
   symbols and the app `REQUIRES esp_wifi`, the project *linked cleanly* (no multiple-definition)
   — but the map + a QEMU boot proved the app called **IDF's** `esp_now_init`, which crashed
   (`Guru Meditation LoadProhibited`) because the real one needs Wi-Fi init. Link order favors
   IDF's archive: once IDF's `esp_now_init` satisfies the symbol, the shim's copy is never pulled.
   *So "it links" is NOT "it interposes" — a trap.*
2. **`--wrap` WORKS.** Renamed the shim's functions to `__wrap_esp_now_*` and added
   `-Wl,--wrap=esp_now_*` (propagated via `target_link_libraries(${COMPONENT_LIB} INTERFACE …)`).
   Verified `--wrap=esp_now_init` reached the final link (build.ninja) and `__wrap_esp_now_init`
   occupies the call slot (map). A QEMU boot then printed the shim's own line:
   ```
   I espnet_shim: esp_now_init (shim): node 1 -> 10.0.2.2:52333
   I spike: reached end of app_main        (clean, no crash)
   ```
   The app's `esp_now_init()`/`esp_now_send()` now call **our** code, with **zero app source
   edits** beyond adding the component + `espnet_shim_start()`.

**Decision.** `--wrap` is the **required** interposition mechanism (not optional). Enabled by
default in `firmware/components/espnet_shim/CMakeLists.txt`; shim functions renamed to
`__wrap_esp_now_*`. The earlier "component provision is enough" note was wrong and is corrected.

**Sources.** brief §9 risk #3, §2(v) (`--wrap`); this machine (ESP-IDF v5.4.2, QEMU 9.0.0).

---

## V-004 — Spike #2 (transport) characterized: connect executes, path UNPROVEN until a HELLO lands

**Context.** brief §9 risk #2: get a frame out of QEMU to a host listener.

**Honesty correction.** An earlier phrasing said "the networking path works too." It does **not**
yet. A `connect()` that returns *failed* fast is not a working path — the transport is unproven
until one HELLO message actually reaches the host listener. What is established is narrower:

**Findings (progression).**
1. First, with a minimal firmware that called `esp_netif_init()` but **never brought up the
   `open_eth` interface or ran DHCP**, `connect(10.0.2.2:52333)` failed instantly (no IP/route) —
   the connect *executed* but nothing reached a host listener.
2. **RESOLVED.** Added the open_eth bring-up to firmware (`CONFIG_ETH_USE_OPENETH`,
   `esp_eth_mac_new_openeth` + `esp_eth_phy_new_dp83848` w/ `autonego_timeout_ms=100`,
   `esp_netif` glue, DHCP), waiting for `IP_EVENT_ETH_GOT_IP` before starting the shim. Booted
   under QEMU with `-nic user,model=open_eth` and a host listener running the **real
   `esp_netsim.protocol` codec** on `127.0.0.1:52333`. Result — the medium decoded genuine
   messages from the firmware:
   ```
   guest: GOT_IP 10.0.2.15 -> connected to medium 10.0.2.2:52333 -> HELLO+TX emitted
   host : RECV HELLO {node:1, mac:aa:bb:cc:00:00:01, caps:{espnow:true,wifi:false}}
   host : RECV TX    {node:1, src:aa:bb:cc:00:00:01, dst:ff:ff:ff:ff:ff:ff, ch:1,
                      rate_mbps:1.0, seq:1, data_bytes:1}
   ```
   Full evidence: `docs/spike2_evidence.log`. This is the **first true QEMU-node ↔ host-medium
   contact**: real ESP-NOW frame → `--wrap`'d `esp_now_send` → lwIP → open_eth → slirp NAT
   (guest 10.0.2.15 → host 10.0.2.2) → host codec. It also confirms the `10.0.2.2`=host-loopback
   mapping end-to-end.

**Decision.** The proven node app is committed as `firmware/examples/espnet_node/` (the shim
component tested *was* the repo's copy, pulled via `EXTRA_COMPONENT_DIRS`). Transport for the
demo is open_eth + user-net NAT.

**Remaining transport work (before timing anything):** swap the JSON wire body for
**length-prefixed binary** — hand-encoding JSON in C on an emulated Xtensa core, in the path we
are about to time, is a smell (D-019). The protocol already frames by length; only the body
encoding changes. Do this *before* measuring per-frame overhead.

**Sources.** brief §1 (open_eth, `esp_eth_mac_new_openeth`, user-net NAT), §9 risk #2; this
machine (ESP-IDF v5.4.2, QEMU 9.0.0).

---

## V-005 — Binary wire body DONE (C↔Python), proven on-target

**Context.** Reviewer step 2: swap JSON for length-prefixed binary *before* timing anything.

**Done.** Replaced the JSON body with a compact big-endian binary body in both the Python codec
(`protocol.encode_body`/`decode_body`) and the C shim (`espnet_shim.c` — raw byte writers, no
JSON, no hex). Outer 4-byte length framing unchanged. Layout locked by
`tests/test_protocol.py::test_binary_wire_layout_is_locked`. Full Python suite still green (56).

Re-verified end-to-end on the emulated esp32s3: the C shim's **binary** HELLO + TX were decoded
by the real host codec, with `t = 1503828` (esp_timer micros, D-022) — not 0 —, `rate_mbps=1.0`
(float32 round-trip C→Python), MACs and seq intact (`docs/spike2_evidence.log`). Two ESP-IDF
strict-warning compile errors were caught and fixed en route (missing `esp_timer` REQUIRES;
`-Werror=misleading-indentation` in a byte-writer loop) — the build is `-Werror`, so the shim now
compiles clean under it.

**Why before the barrier spike:** hand-encoding JSON in C on the emulated core, in the path about
to be timed, would contaminate the latency numbers (reviewer). Binary removes that variable.

**Sources.** Reviewer step 2; D-019.

---

## D-022 — Guest virtual-time source: the TX `t` field = guest `esp_timer` micros

> **Superseded by V-010 / D-029.** The guest clock does freeze on `stop`, but while running it
> does not advance at the host rate, and two identical guests drift apart. The medium no longer
> uses `t` for QEMU frames.

**Context.** The first real TX from firmware carried `t: 0` (the shim hardcoded it). Before the
barrier spike, the guest↔medium time relationship must be decided, because it shapes the whole
barrier design (reviewer). Options: (a) guest's own `esp_timer_get_time()` µs; (b) echo the slice
boundary the medium handed it; (c) nothing — medium stamps arrival.

**Decision.** (a) — the guest stamps `esp_timer_get_time()` microseconds into the TX `t` field,
and the medium treats that as the frame's **virtual time** directly.

**Rationale — why this is not arbitrary.** Under the planned QMP `stop`/`cont` barrier (D-004),
QEMU's guest clock (and the SysTimer/`esp_timer` backed by it) **freezes on `stop` and advances
only during `cont`**. So the guest's elapsed `esp_timer` time equals the *accumulated running
time* = exactly the virtual time the medium has granted it (`Σ cont durations`). Guest time and
medium virtual time are therefore the **same clock**, reconciled by construction — no offset
tracking, no drift model needed. This is the cleanest possible answer and it makes option (b)
redundant (the slice boundary is derivable) and (c) inferior (arrival-stamping discards the
sub-slice ordering the guest clock preserves). Caveat to verify in the spike: this holds only if
the fork's guest clock is `stop`-frozen (expected) — if any timer advances while paused, revisit.

**Consequences.** The shim now calls `esp_timer_get_time()` for the TX `t` (replacing the `0`).
The medium's causality rule (D-014: delivery `t` ≥ frame `t`) then operates in one consistent
clock. `caps.wifi` stays **false** until M4 — kept honest.

**Sources.** Reviewer guidance; QEMU QMP `stop`/`cont` semantics (brief §4); D-004/D-014.

---

## V-006 — Clock-freeze VERIFIED: QMP `stop` freezes the guest esp_timer (D-022 holds)

**Context.** Before trusting any barrier latency number, verify the load-bearing assumption
(reviewer step 1): does the fork's guest clock actually freeze on QMP `stop`? If not, D-022 —
and the barrier design — collapses.

**Method.** `firmware/qemu/clock_freeze_probe.py`: a firmware logs `esp_timer_get_time()` every
200 ms over serial; the harness `cont`s the VM, collects samples, then QMP `stop`s for a known
wall interval, `cont`s, and collects more. Compares guest-time vs wall-time advance across the
pause.

**Result.**
```
QMP status after stop: {'status': 'paused', 'running': False}
straddling sample pair: host_gap = 3.27 s   guest_gap = 0.220 s
=> guest advanced 0.22 s while wall advanced 3.27 s   RESULT: FREEZE_CONFIRMED
```
The guest `esp_timer` advanced by one normal loop step (0.22 s) while the VM sat paused for
3.27 s of wall time. **The guest clock is stop-frozen**, so `esp_timer` == accumulated virtual
time (D-022 validated). The barrier can use it directly with no offset/drift model.

**A harness bug caught by insisting on this step (honesty).** The first run reported
`FREEZE_VIOLATED` — but dumping the raw samples showed they *all* predated the pause: the serial
socket inherited a 1 s timeout from `create_connection`, so the reader thread died 1 s into the
3 s pause and never captured the post-resume samples. Fixed (blocking socket, tolerate timeouts).
Lesson: a null result from a timing harness must be corroborated by raw data before it is
believed — exactly why "verify first, as its own step" was the right instruction. The spurious
result was not quietly accepted.

**Sources.** Reviewer step 1; QEMU QMP `stop` semantics; D-022; this machine.

---

## D-023 — Barrier spike (risk #1) pass/fail criterion, written BEFORE running it

**Context.** To avoid interpreting whatever comes out as "acceptable," the threshold is fixed in
advance (reviewer).

**Pass criteria (one real QEMU node gated by QMP `stop`/`cont`, ESP-NOW ping):**
1. **Latency fidelity:** median measured one-way latency within **±20%** of the medium's computed
   value for the same frame.
2. **Throughput:** sustain **≥ 100 frames/second** with one node under the barrier.

**If it fails:** fall back to the already-documented **deterministic offline mode** (run-to-
barrier without real-time pacing) — this is the correct answer, not a defeat (D-004 thresholds).
Expect it to be tight: a 1 ms slice against a ~1 ms MAC latency gives ~one quantum of resolution
on the very quantity being measured, so a finer slice (e.g. 250 µs) is the first knob to try.

**Sources.** Reviewer guidance; brief §3 (ESP-NOW ~1 ms MAC latency), §9 risk #1.

---

## D-024 / V-007 — Barrier throughput MEASURED: passes D-023 (over a UNIX socket)

**Context.** D-023 throughput axis: can the QMP `stop`/`cont` barrier sustain ≥100 slices/s with
one real node without collapsing throughput? Measured with
`firmware/qemu/barrier_overhead_probe.py` (one esp32s3 node, N=200 cycles per point).

**Result (UNIX-socket QMP — the transport the real `qemu_launcher.py` uses):**

| target slice | wall/cycle | QMP overhead | realtime factor | slices/s |
|--------------|-----------|--------------|-----------------|----------|
| floor (0)    | 745 µs    | 745 µs       | —               | **1343** |
| 1000 µs      | 2374 µs   | 1374 µs      | ×2.4 slower     | **421**  |
| 250 µs       | 1088 µs   | 838 µs       | ×4.4 slower     | **919**  |

**Verdict on the throughput axis: PASS.** 421 slices/s at a 1 ms slice is 4× the ≥100/s bar;
even 250 µs sustains 919/s. The per-cycle QMP `stop`/`cont` overhead is ~0.7–1.4 ms, so a 1 ms
barrier runs at ~2.4× slower than real time — usable, and the deterministic offline mode removes
even that pacing if wanted. As expected (reviewer), a 250 µs slice is overhead-dominated (×4.4),
so 1 ms is the better default; the slice/fidelity curve is recorded here for the paper.

**Two harness artifacts caught and ruled out (honesty — the numbers were nearly reported wrong
TWICE):**
1. **TCP delayed-ACK.** The first measurements showed **88 ms/cycle, 11 slices/s** — which would
   have "failed" D-023 and condemned the barrier. That was a TCP artifact: QMP over a TCP socket
   stalls ~2×40 ms on Nagle/delayed-ACK per `cont`+`stop`. Setting `TCP_NODELAY` on the client
   did *not* fix it (the stall is QEMU's send side), but switching QMP to a **UNIX socket**
   dropped it ~100× to 745 µs. The real design already uses a UNIX socket, so 745 µs is the
   number that matters — but I nearly logged 88 ms as "barrier collapses throughput." Ruled out.
2. **pkill footgun.** `pkill -f qemu-system-xtensa` silently killed the *shell running the probe*
   (its command line contained that string), producing empty output that looked like a hang.
   Fixed by matching the exact process name (`pkill -x qemu-system-xte`).

Combined with **V-006** (clock freeze confirmed), the two load-bearing barrier assumptions —
guest-clock-freezes and throughput-doesn't-collapse — are both **validated on real hardware
emulation**. The remaining axis of D-023 (latency *fidelity*: median within ±20% of the medium's
computed value) needs the two-node barrier↔medium integration and is the next sub-task.

**Sources.** Reviewer step-3 (record 1 ms and 250 µs); D-023; D-022/V-006; this machine.

---

## V-008 / D-025 — Latency fidelity MEASURED: risk #1 retired (needs a 250 µs slice)

> **Revised by V-010.** This modeled the slice as exactly 250 µs of guest time. Measured end to
> end, a slice is 1.5–4 ms of guest time because the guest keeps running through the QMP round
> trips, so the 14.3 % figure does not describe what firmware observes.

**Context.** The last axis of D-023: is the ESP-NOW latency a node observes under the barrier
within ±20% of the medium's computed value? This exercises D-014 (barrier dispatch quantization)
end-to-end with a real node.

**Method.** `firmware/qemu/barrier_latency_probe.py`: a real QEMU node (burst-sends 400 unicast
ESP-NOW frames) is gated by the QMP barrier while the host collects each frame's real guest
`esp_timer` `t` (= virtual time, D-022). For each candidate slice S, the barrier dispatches a
frame at the next boundary ≥ `t + airtime`, so the *reactive* latency a receiver can act on is
`ceil((t+airtime)/S)·S − t`. Compared to the medium's computed `airtime` (908 µs for a 16-byte
acknowledged unicast). 300 real frames collected.

**Result.**

| slice | computed | effective (measured) | median error | verdict |
|-------|----------|----------------------|--------------|---------|
| 1000 µs | 908 µs | 1384 µs | **52.5 %** | FAIL |
| 250 µs  | 908 µs | 1038 µs | **14.3 %** | PASS |

**Verdict.** Exactly the reviewer's prediction: the ~0.75 ms barrier overhead is the same order
as the ~0.9 ms MAC airtime, so a 1 ms slice's dispatch quantization (mean +½ slice) fails ±20%.
A **250 µs slice passes both axes**: fidelity 14.3 % (V-008) *and* throughput 919 slices/s (V-007).

**Decision (D-025): default the barrier slice to 250 µs for ESP-NOW latency experiments**
(overriding the 1 ms starting default in D-004), accepting ×4.4-real-time pacing. Two escape
hatches make the risk fully retired:
- **Trace fidelity is always exact:** the medium stamps RX/pcap with the *computed* delivery time
  (D-016/Wokwi lesson), so timing *analysis* is correct at any slice; only the *reactive* loop is
  quantized.
- **Deterministic offline mode** (run-to-barrier, no real-time pacing, D-004 fallback) can advance
  virtual time straight to each delivery instant — **zero quantization, perfect fidelity** — for
  experiments that need it, trading away real-time interactivity.

**Honesty bounds on this number.** The measured value is the **one-hop dispatch quantization**
(sender→medium→dispatch), the dominant fidelity error. A full sender→receiver→app round trip adds
the receiver's own processing-slice, so 14.3 % at 250 µs is a *lower bound* on end-to-end reactive
error; if a two-hop experiment needs tighter, use offline mode. The 1 ms FAIL only gets more
decisive under that compounding.

**Risk #1 status: RETIRED.** Clock-freeze holds (V-006), throughput passes (V-007), latency
fidelity passes at a 250 µs slice (V-008), with offline mode as the exact-fidelity fallback. No
redesign forced — the barrier architecture (D-004) stands.

**Sources.** Reviewer (predicted the tight ratio); D-014, D-022, D-023; this machine.

---

## D-026 — Mixed barrier: QMP-gated QEMU nodes + message-gated MockNodes together

**Context.** First honest M2 needs a real QEMU node and (at least) one other node on one shared
barrier. The clock master previously gated every connected node with RUN_SLICE/SLICE_DONE (mock
only). QEMU nodes can't ack SLICE_DONE from inside firmware in v1 — they're gated externally via
QMP `stop`/`cont`.

**Decision.** Introduce a `NodeGate` abstraction (`orchestrator/gate.py`). Per slice the clock
master releases **all** participants concurrently and waits for **all** to reach the barrier:
- **message-gated** (MockNode): `RUN_SLICE(until)` → node emits due TX → `SLICE_DONE` (unchanged);
- **QMP-gated** (`QmpGate`, QEMU): `cont` → run the guest for `slice_us` wall → `stop`. The guest
  clock freezes on `stop` (V-006), so `slice_us` of wall ≈ `slice_us` of virtual time.

Message-gated participants = connected nodes minus registered QMP gates, so with no QMP gates the
behavior is byte-identical to before (existing tests unchanged).

**Frame timing (v1 simplification, honest).** Frames from a QEMU node carry the guest's own
`esp_timer` `t`, which has a per-node boot offset vs. the medium's virtual clock. For M2
(controlled *delivery*, not sub-slice latency) this offset is immaterial — the medium processes
each slice's frames as one batch (D-013/D-014). Precise cross-node latency needs a per-node
offset alignment (medium-virtual − node-`esp_timer`, constant under the lockstep barrier); that
is deferred and already flagged in V-008's honesty bounds. A typical M2 has one sender and a
non-transmitting receiver, so heterogeneous timestamps don't cause spurious cross-node collisions.

**Sources.** D-004 (barrier), D-014 (dispatch), D-015 (protocol parity), V-006/V-007/V-008; brief
§9 (M2).

---

## V-009 / M2 — First honest M2: real QEMU firmware delivers ESP-NOW across the barrier

**Context.** M2 ("two-node link, controlled delivery") had been demonstrated only with two
MockNodes. This is M2 with **real ESP-IDF firmware** on one side — the milestone made honest.

**Setup.** `firmware/qemu/m2_run.py`: a real ESP32-S3 node (`m2node` firmware: open_eth+DHCP +
shim, broadcasts ESP-NOW every 20 ms) is registered as a **QMP-gated** barrier participant
(1 ms slice) via `QmpGate`; a **message-gated** `MockNode` receiver at 5 m shares the same clock
master and medium (lossless `ConstantErrorModel(0)`). Both run under one `Simulation` barrier
(D-026).

**Result (real run, WSL2 / QEMU 9.0.0):**
```
connected nodes at end: [1, 2]      QEMU node (1) HELLO'd to medium: True
mock receiver (2) got 140 ESP-NOW frames from the QEMU node
  first RX: src=aa:bb:cc:00:00:01  rssi=-41.0 dBm  data0=0
medium stats: tx=140 success=140 rx_delivered=140
M2_RESULT: PASS — real firmware ESP-NOW delivered across the barrier
```

**What this proves.** A real firmware ESP-NOW broadcast, gated in lockstep virtual time with a
second node, flows through the *actual medium physics* and is delivered: the `rssi = −41.0 dBm`
is the log-distance model's computed receive power for the 5 m link (Ptx 20 dBm − PL(5 m)=61 dB =
−41 dBm, D-006) — the medium is genuinely applying its path-loss model to the emulated firmware's
frames, not passing them through. Both node kinds ran on one barrier (D-026), confirming the
single-protocol payoff (D-015): the medium can't tell the real node from the virtual one.

**Honest scope.** Delivery + medium-physics are proven end-to-end; this run uses a lossless link
(controlled delivery), one real + one virtual node, and does not yet measure two-real-node
reactive latency (that needs the clock-alignment noted in D-026/V-008). Next: a `range`/`snr_per`
link to show controlled *loss*, and two real QEMU nodes.

**M2 status: DONE with real firmware.**

**Sources.** brief §9 (M2 "controlled packet delivery/loss"); D-006, D-013, D-015, D-026.

---

## D-027 — Deslop pass: cut speculative surface before the two-node work

Applied `CLAUDE.md` / `.claude/commands/deslop.md` (working rules: no unused code, no
one-impl abstractions, one-line docstrings). Deletions, grep-verified caller-less:

- `RangeThresholdErrorModel` (+ its config branch, `tx_range_m`/`success_ratio`, 2 tests). No
  scenario used it. This overrides D-007's "three models": the `ErrorRateModel` seam stays, so
  the Cooja-UDGM impl is a cheap re-add when a scenario needs it — two demonstrated impls
  (constant, snr_per), not a speculative menu.
- `Message.to_json_bytes`/`from_json_bytes` (JSON debug path, dead after the wire went binary).
- `QemuNodeSpec` + `QemuNodeHandle` (dead: `m2_run.py` launches QEMU itself and uses `QmpClient`
  directly; the guestfwd `build_argv` was never the validated path). `qemu_launcher.py` 155→46.
- `PropagationLoss` and `NodeGate` Protocols (one impl each) collapsed into concrete types.
- `linear_to_db`, `protocol.del_peer`, `espnet_shim_on_run_slice` (C), `query_status`; internal
  validation guards (`ConstantErrorModel`/`tx_status`); config fields no YAML sets
  (`reference_loss_db`, `shadowing_sigma_db`, `bandwidth_hz`, `max_data_len`).
- Multi-paragraph docstrings trimmed to one line; decorative section boxes removed. Kept the
  load-bearing notes (`--wrap` requirement, slirp 10.0.2.2, S3 bootloader 0x0, wire-layout test).

Net: Python 3185→2682 (−503), C 496→483 (−13); 56 tests (was 59; the three deleted covered
deleted subjects). Kept `ErrorRateModel` (two impls, meets the two-caller bar) and `_TAG_INV`
(used every decode).

---

## D-028 — One firmware image, many nodes: identity in NVS, unmodified apps

**Context.** M2 so far ran one real node, with its node ID compiled in (`ESPNET_NODE_ID`). The
brief's workflow ("firmware A on nodes 1-10") needs one build to serve many nodes, and the
firmware should be "the same, or as close as practical to the same" as on hardware (PDF).

**Options.** (a) Rebuild per node with a `-D` flag: slow, and 20 builds for 20 nodes. (b) Patch
bytes in the app image: breaks the image checksum and SHA-256. (c) Write a per-node NVS
partition. (d) Let the medium assign IDs by connection order: fragile once nodes boot in
parallel.

**Decision.** (c). The runner merges each node's ESP-IDF build into a 4 MB flash image and
writes an NVS partition (namespace `espnet`: `node_id`, `mac`, `channel`, `port`) at the
offset the build's own partition table gives. The NVS partition is not hash-checked, and NVS
is where a real deployment would keep identity. It is generated with Espressif's
`esp-idf-nvs-partition-gen` (pip) rather than by hand-encoding NVS pages.

The shim now starts itself inside `esp_now_init` (NVS identity, open_eth + DHCP, connect to the
medium, HELLO), and `--wrap`s the Wi-Fi bring-up an ESP-NOW app makes on hardware
(`esp_wifi_init`, `set_storage`, `set_mode`, `start`, `set_channel`) into no-ops. An app written
exactly like Espressif's ESP-NOW example therefore builds and runs unmodified; only the project
`CMakeLists.txt` adds the shim component. `-DESPNET_HARDWARE=1` builds the same app for boards.

Shim rewrite, per `CLAUDE.md`: packed structs for every wire message instead of hand-written byte
writers; the transport folded into the shim (one backend, no interface); static FreeRTOS
allocation. Two bugs fixed on the way: the receive callback was handed raw MAC bytes cast to
`esp_now_recv_info_t*` (any app reading `info->src_addr` would crash); it now gets a real struct
with `rx_ctrl->rssi`. And the length prefix and body went out as two `send()` calls with Nagle
on; now one call with `TCP_NODELAY`.

Firmware examples: A `espnow_ping` (broadcast a timestamped ping every 100 ms, log each reply's
RTT), B `espnow_pong` (answer each ping with a unicast reply, from a task, since sending from the
receive callback is discouraged on hardware). `espnet_node` is deleted; A replaces it.

**Sources.** PDF ("same firmware"); ESP-IDF NVS and ESP-NOW example; D-017, D-021.

---

## D-029 / V-010 — Guest clocks are not host time under stop/cont; stamp by slice instead

**Context.** First run of two real nodes (M2, ping/pong, 250 µs slices). The pong's reply went
out 19–27 ms after the ping was delivered, and the lag grew with run time.

**Measurement 1: the slice is not 250 µs of guest time.** 6 s of virtual time took ~3,000 slices:
each "250 µs" slice advanced the guests ~2 ms. The guest keeps running through the QMP `cont`
and `stop` round trips (V-007 measured ~0.8 ms of overhead per cycle for one node; with several
nodes it is more). `slice_us: 50` gave the same slice count and the same RTT, so below the QMP
floor `slice_us` does not matter. The CLI now prints the effective quantum; measured:
2.0 ms (2 nodes), 2.4 ms (5), 2.1 ms (3, Wi-Fi), 3.7 ms (20).

**Measurement 2: guest clocks drift from host time and from each other.** Logging each TX's
guest `t` against the host's own estimate of running time (reply-to-reply of `cont` and `stop`):
node 1's `esp_timer` advanced at 0.96 of the host estimate and node 2's at 0.85, and by the end
the two guests' clocks were 200–350 ms apart. Both run identical boot code in the same host
windows, so D-022's premise (`esp_timer` equals granted virtual time) does not hold across
nodes. Separately, the FreeRTOS tick falls behind `esp_timer` under stop/cont, so `vTaskDelay`
and log timestamps stretch (a 100 ms ping loop measured 130–150 ms by `esp_timer`).

**Decision.**
1. The medium stamps a QEMU node's frame with the virtual time of the slice it arrived in, not
   the guest's `t`. Sub-slice ordering is lost, but every node shares one timeline.
2. Virtual time advances by the measured guest running time per slice (mean over nodes of
   `stop` reply minus `cont` reply; QEMU applies both just before replying), not by the nominal
   `slice_us`.
3. Every delivery (RX, TX_STATUS, Wi-Fi forward) is held in a queue until its computed virtual
   time and released at the first slice boundary at or after it. Before, a frame was delivered
   at the end of the slice it was sent in, even when its airtime outlasted the slice.
4. Frames that reach the host between slices (slirp is asynchronous) join the next batch; before,
   the per-slice reset dropped them.

**Consequence.** Reactive latency is quantized to the effective slice: M2 RTT (ping, broadcast,
unicast reply, 20 % loss) p50 5.6 ms, min 3.3 ms, n = 28; real hardware would be ~2–3 ms.
Delivery, loss, retries and ordering are unaffected. Firmware runs are not bit-reproducible
even with a fixed seed, because guest timing changes the order frames reach the medium; mock
runs still are.

**Sources.** This session's M2 runs; `simulation.py` `_run_qemu`, `_start_time`, `_flush_due`.

---

## V-011 — UART on a Windows-mounted path stalls the guest

With the ping's `t0` taken before its log line, `t0` to the shim's send stamp was 4–34 ms: the
`ESP_LOGI` call. QEMU wrote the UART to a file under `/mnt/c` (a 9P mount), and the guest blocks
on every UART write. UART logs now go to the run's temp dir on the Linux filesystem and are
copied to `out/` at the end. The ping app also takes `t0` after logging. Median RTT went from
13.7 ms to 4.6 ms.

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
start stamp (D-029), and collided at node 1; node 2 captured and 3 and 4 got 16/36 and 13/36
replies through. On hardware those three hear each other and defer.

**Decision.** (1) Carrier sense: before evaluation, each frame is delayed past any earlier
same-channel frame whose sender it can hear at or above −82 dBm (802.11 preamble detection).
Hidden terminals still collide. (2) Each unicast retry is checked against the traffic overlapping
that attempt's own window. Before, all retries reused the first attempt's interferers, so one
collision doomed all four attempts.

**Result.** M3 replies to node 1: node 3 16/36 → 34/34, node 4 13/36 → 35/35 (20 of node 4's needed
a retry: at −88 dBm it is a hidden terminal to node 2). Tests: hidden terminals collide; senders in
range take turns; a unicast escapes a hidden-terminal collision on its retry.

---

## D-032 / V-012 — M4: Wi-Fi stations reach the real Internet across the modeled link

**Context.** PDF component 5: stations associate with a virtual AP; traffic that crosses the
simulated link goes through the host to the real Internet (DNS, TCP, HTTP).

**Design.**
- Guest: the shim emulates a Wi-Fi station on the open_eth NIC.
  `esp_netif_create_default_wifi_sta` returns the Ethernet netif; `esp_wifi_start` posts
  `WIFI_EVENT_STA_START`; `esp_wifi_connect` starts Ethernet; DHCP success is re-posted as
  `WIFI_EVENT_STA_CONNECTED` and `IP_EVENT_STA_GOT_IP`. Firmware C (`wifi_http`) is a standard
  IDF station app that fetches `http://example.com` every 2 s, unmodified.
- Host: QEMU's `filter-redirector` detours every Ethernet frame between the NIC and slirp through
  four UNIX-socket chardevs to the medium (`wifi_link.py`). Each frame becomes an 11 Mbps unicast
  on the station-AP link (the AP is medium node 0 and ignores ESP-NOW); if the medium delivers it,
  it goes on to slirp (uplink) or the guest (downlink) at its computed time. It contends with
  ESP-NOW on the same channel and gets carrier sense, retries and loss like any frame. The shim's
  own TCP link to the medium (10.0.2.2:port) is exempt: it is not radio traffic.
- Trace: Wi-Fi frames are written as 802.11 data frames (ToDS/FromDS, LLC/SNAP), so Wireshark
  decodes the real IP, TCP and HTTP inside.

**Result (`configs/m4_wifi_gateway.yaml`, AP on channel 6):**

| station | distance | RSSI | outcome |
|---------|----------|------|---------|
| 1 | 25 m | −62 dBm | DHCP, then HTTP 200 every 2 s (21–88 ms) |
| 2 | 160 m | −86 dBm | DHCP, then HTTP 200 every 2 s; retries in some runs (up to 4 attempts) |
| 3 | 230 m | −91 dBm | every DHCP frame lost after 4 attempts; logs "no IP yet" |

**Two bugs found on the way.** (1) QEMU closed the filter sockets at once: `bridge()` kept only
one half of each asyncio connection, and a collected `StreamWriter` closes its socket. (2) A new
app built for the default ESP32 target (bootloader at 0x1000) boot-looped; apps now pin
`CONFIG_IDF_TARGET="esp32s3"` and scenario loading rejects builds for any other chip.

**Honest scope.** There are no 802.11 authentication or association frames: "associated" means
the link is usable. Each station has its own slirp NAT, so stations cannot reach each other at
layer 2 through the AP. Wi-Fi rate is fixed at 11 Mbps.

**Sources.** PDF component 5 and M4; QEMU `filter-redirector` docs; D-012, D-031.

---

## D-033 — M5 and M6: the scenario file launches everything; each run leaves a replay

**M5.** A node with `firmware: <build dir>` boots under QEMU; one without is a mock. `esp-netsim run`
builds each firmware node's flash image, starts every QEMU paused, connects QMP (and the Wi-Fi
sockets), runs the barrier, then writes `out/<name>/`: `trace.pcapng`, `results.json` (topology,
totals, one row per frame and receiver), each node's UART log, and `report.html`.
`orchestrator/gate.py` and `firmware/qemu/m2_run.py` are gone: the barrier calls `cont`/`stop`
on the QMP clients directly, and the runner replaces the one-off M2 script.

**M6.** `report.html` is a self-contained page (no network, no build step) with exactly what the
PDF lists: nodes at their positions, links coloured by delivery ratio, each frame animated
green or red as a timeline plays or is scrubbed, and each node's console revealed in step. The
console is placed by the firmware's own log timestamps, which lag virtual time (D-029).

---

## V-013 — The brief's 20-node workflow, and scale

`configs/example_20_nodes.yaml`: a 4 × 5 grid at 100 m; firmware A on nodes 1–10, B on 11–18,
C on 19–20 with the AP. 20 QEMU VMs, 5 s of virtual time in 25 s of wall time on this laptop
(WSL2, 20 threads). The effective quantum is 3.7 ms. 694 transmissions; both Wi-Fi stations
fetched example.com; node 1 heard no pong because none is within range of its corner, while
node 10 in the middle logged 97 RTTs in the development run.

---

## V-014 / D-034 — First hardware measurement: a unicast nobody ACKs takes ~35 attempts

**Setup.** One ESP32-S3 board (rev v0.2, CH343 USB serial), ESP-IDF v5.4.2, indoors on channel 1
with ordinary home Wi-Fi around. Firmware A built with `-DESPNET_HARDWARE=1` boots unmodified and
pings every 100 ms. With one board there is nothing to answer, so the A/B checks of Appendix E
wait for a second board. What one board can measure is the send callback, which the medium
models: firmware D (`espnow_txprobe`) times `esp_now_send` to its callback, one send in flight,
for a broadcast and for a unicast to `02:00:00:00:00:99` (a registered peer no device holds),
at 16, 128 and 250 bytes. The same image runs in the emulator as `configs/hw_txprobe.yaml`.

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
assumes. The board adds ~400 µs per send that the airtime model does not have (driver path);
the emulator hides it under its 1.5–4 ms quantum (V-010). A failed unicast grows 274 µs per byte
at the minimum and 284 at the median. Each transmission costs 8 µs per byte, so the radio sends
the frame about 35 times before reporting FAIL. The medium had two errors here. It allowed
4 transmissions (`max_retries: 3`, from the brief), and for a destination no node holds it
reported `peer_not_exist` at once without radiating. A real sender cannot know nobody is there.

**Decision.**
1. `max_retries` defaults to 34 (35 transmissions).
2. A unicast to a MAC no node holds radiates every attempt and fails as `no_receiver`.
3. A frame stays in flight for all its attempts, not just the first, so neighbours defer
   through a whole retry sequence (a failed 250-byte unicast holds the channel ~100 ms).

**Residual.** With the same airtime model, the expected time to fail is 37.0 / 68.4 / 102.6 ms.
The board measures 1.03–1.09× that; the ~15-byte ESP-NOW vendor header the model omits accounts
for about 4 ms of it. The emulator measures 0.88–0.90×: the medium schedules the callback at the
right virtual time, but the guest's `esp_timer` advances slower than the host time the barrier
credits, the drift V-010 already measured (0.85–0.96). Closing that needs the guest clock itself
to set virtual time, which is the `-icount` item in Appendix C. Retries for Wi-Fi data frames
use the same default but were not measured; on hardware they also go through rate control.

**Sources.** This session's board and emulator logs; `firmware/examples/espnow_txprobe`.

---

## V-015 — V-014 repeated on a second board

**Setup.** With the V-014 board (MAC `d4:05:92:7b:14:fc`) unavailable, a different ESP32-S3
(rev v0.2, CH343, MAC `d4:05:92:79:90:c8`, 8 MB PSRAM) ran the same `build_hw` image of
firmware D, channel 1, on 2026-10-20. All 600 sends logged (n = 100 per row).

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
(1 Mbps). Medians and tails are lower on every row, broadcast included, so the channel was
quieter during this run; that is carrier-sense deferral, not the radio. A failed unicast
grows 260 µs per byte at the minimum and 268 at the median, i.e. 32–34 transmissions against
V-014's 34–35. Across two boards the retry count is 33–35 transmissions; `max_retries: 34`
stays. Against the model's expected time to fail (37.0 / 68.4 / 102.6 ms) this board reads
0.87–0.93×, where V-014 read 1.03–1.09×. The V-014 excess was channel load in that run, so
the emulator's 0.88–0.90× sits inside the spread of the two boards and the guest-clock drift
in V-014's residual is not separable from channel load with these data.

**Sources.** This session's board log; same firmware and scripts as V-014.

---

## V-016 — A/B on two boards: RTT vs payload, board vs emulator

**Setup.** Two ESP32-S3 boards side by side on one PC, RSSI −17 to −30 dBm: firmware A on
`d4:05:92:7b:14:fc` (COM6), firmware B on `d4:05:92:79:90:c8` (COM8), both built with
`-DESPNET_HARDWARE=1`. Firmware A takes a build option `PING_LEN` (default 13, the bare
`ping_msg_t`) and B now echoes whatever length arrives. 60 s per size, 600 pings each.
The emulator runs the same sources as `configs/hw_ab.yaml`: one pair per size, pairs 2 km
apart so they never hear each other (every cross-pair copy dropped), 20 s, ~160 pings each.

**Result** (RTT, µs; no ping lost and no reply FAILED on either side):

| len | board p50 | board p95 | board mean | emulator p50 | emulator mean | emulator − board |
|---|---|---|---|---|---|---|
| 13 | 3,112 | 5,528 | 3,513 | 5,989 | 6,168 | +2,655 |
| 128 | 4,911 | 6,827 | 5,277 | 5,945 | 6,251 | +974 |
| 250 | 6,920 | 9,418 | 7,370 | 9,545 | 9,521 | +2,151 |

**Reading.** On the boards RTT grows 16.3 µs per byte, two 1 Mbps transmissions of 8 µs per
byte, so the airtime model matches the radio on both legs. The emulator reads high by 1.0–2.7 ms,
0.4–1.2 of its 2.2 ms effective quantum in this six-node run (1.8 ms with two nodes, where the
13-byte gap was 1.3 ms). It cannot see the 13 → 128 byte step at all (+83 µs against the boards'
+1.8 ms) because both round trips fit in the same number of quanta; 250 bytes crosses one more
boundary and jumps by a full quantum. So the emulator's latency is right only to within one
quantum, and differences smaller than a quantum vanish. That is Appendix E's prediction, now
measured, and it puts timing resolution (Appendix C item 2) ahead of everything else.

A first 128-byte run had 9 % of RTTs between 10 and 88 ms with no failed reply; a repeat ten
minutes later had none above 15 ms. The repeat is in the table; the first run's tail was
other traffic on channel 1.

**Sources.** This session's board and emulator logs; `firmware/examples/espnow_ping`,
`espnow_pong`, `configs/hw_ab.yaml`.

---

## D-035 — The barrier's sleep was most of the quantum

**Measurement.** Timing each part of a barrier cycle (two nodes, `slice_us: 250`, 5 s):
QMP `cont` round trip 0.46 ms, the wait between them 1.20 ms, `stop` round trip 0.69 ms;
the guests ran 1.87 ms per cycle. The wait was `asyncio.sleep(0.00025)`, which the event loop
rounds up to epoll's 1 ms timeout, and the guests run through all of it.

**Decision.** The barrier waits with a blocking `time.sleep`. Frames that reach the medium
during the wait sit in the socket buffer and are read during the `stop` round trip, or join the
next batch as before (D-029). This supersedes D-029's finding that `slice_us` below the QMP
floor does not matter: the floor was the sleep.

**Result.** Effective quantum 2.0 → 0.85 ms (2 nodes), 2.2 → 1.26 ms (6), 1.16 ms on
`m3_espnow_line`. With two nodes, `slice_us` 100 and 20 give 0.60 and 0.52 ms at nearly the
same wall time per virtual second; ~0.5 ms is now the QMP round trips. On `hw_ab` (V-016) the
emulator's mean RTT minus the boards' went from +2.7 / +1.0 / +2.2 ms to +1.1 / +1.4 / +0.9 ms
at 13 / 128 / 250 bytes, and RTT now grows 15.6 µs per byte against the boards' 16.3, so the
emulator resolves payload size. At `slice_us: 100` (quantum 0.99 ms) the 13-byte median is
unchanged (4.58 against 4.39 ms), so the remaining ~1 ms is not the quantum. Likely the
shim's transport (each TX and RX crosses lwIP and slirp while the guest runs); not yet measured.
62 tests pass on three consecutive runs, the wall-clock-sensitive barrier test included.

---

## Status snapshot (end of this session)

- M1–M6 run with real firmware (M1 in every scenario, M5 and M6 for every run). Scenarios:
  `m2_two_qemu`, `m3_espnow_line`, `m4_wifi_gateway`, `example_20_nodes`, `hw_txprobe`, `hw_ab`;
  mock-only `two_node`, `multi_node_espnow`.
- 62 tests (medium, barrier, wire codec, config, trace, Wi-Fi bridge, runner end to end).
  `test_mixed_barrier_qemu_sender_to_mock_receiver` is wall-clock sensitive and fails about
  one run in three, before and after D-034.
- Hardware: send-callback timing and retry count on two boards (V-014, V-015); A/B RTT vs
  payload on two boards together (V-016). PDR vs distance needs the boards moved apart.
- Limits: timing quantized to 0.85–1.3 ms of virtual time (D-035); RTT reads ~1 ms high
  against boards for a reason not yet measured (D-035); no 802.11 association frames (D-032).

---


---

# PART II — Consolidated Reference

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
  QEMU nodes; wait for mock acks and the slice's wall time; `stop`; advance virtual time by the
  measured guest time; hand the slice's frames to the engine; queue each outcome's deliveries at
  their computed times; release everything now due.
- `medium.py` holds positions, channels and peers and decides each frame: carrier-sense deferral,
  per-receiver SINR against overlapping frames with capture, per-attempt retries for unicast,
  ESP-NOW send-callback semantics (D-012).
- `channel/`: log-distance path loss (D-006), SNR→PER (D-007), 802.11 DCF airtime (D-008).

**Determinism.** One seeded RNG. A mock-only run is a pure function of scenario and seed. A run
with QEMU nodes is not bit-reproducible: guest timing changes the order frames reach the medium.

## Appendix B — Node ⇄ Medium Wire Protocol

TCP to `127.0.0.1:<port>` (the guest reaches it as `10.0.2.2:<port>` through slirp). Every message
is a `uint32` big-endian length and a binary body: `uint8` tag, then fixed big-endian fields;
MACs are 6 raw bytes, payloads `uint16` length + bytes, floats IEEE-754 `float32`. The
authoritative layout is `protocol.py` (`encode_body`/`decode_body`), mirrored by the packed
structs in `espnet_shim.c` and locked by `tests/test_protocol.py`.

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

## Appendix C — Milestone Status

| M | Goal (PDF) | Status | Evidence |
|---|---|---|---|
| M1 | Boot an ESP-IDF image under QEMU, capture UART | done | every firmware run; `docs/m1_qemu_boot.log` (D-020) |
| M2 | Two emulated nodes, controlled delivery and loss | done | `m2_two_qemu`: 20 % constant loss; 5 of 34 pings lost, replies retried and delivered (D-029) |
| M3 | Several nodes, configurable ESP-NOW connectivity, traces | done | `m3_espnow_line`: delivery falls off with distance, carrier sense and hidden terminals (D-031) |
| M4 | Wi-Fi to the real Internet and back | done | `m4_wifi_gateway`: HTTP 200 near, retries at the edge, no DHCP out of range (D-032) |
| M5 | Config file launches all nodes, collects results | done | `esp-netsim run` (D-033) |
| M6 | GUI: nodes, links, packets, console | done as a replay page | `out/<name>/report.html` (D-033) |

**Next, in order.**
1. Find the ~1 ms RTT excess left after D-035 (timestamp the shim's TX and RX path in the
   guest). Below that, `-icount` or a run-to-next-event barrier for the ~0.5 ms QMP floor.
2. PDR vs distance on boards moved apart (Appendix E); RTT vs payload is done (V-016).
3. 802.11 association frames and station-to-station traffic through one shared AP.
4. A live (not replay) view, and topology editing in the GUI (PDF: "configuring topology").

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

## Appendix E — Validation Plan

Done, no hardware: V-001 (emergent ~200 m range matches published ESP-NOW LOS range);
unit reference points (Friis reference loss, DCF constants, airtime); M2–M4 behave as the model
predicts (loss rate, distance falloff, retries, hidden terminals).

Done, one board (V-014, repeated on a second in V-015): firmware A boots unmodified on an
ESP32-S3; send-callback latency for broadcast and for an unanswered unicast, 16–250 bytes, board
vs emulator. This set the retry count to 35 transmissions and made an unanswered unicast radiate.

Done, two boards (V-016): RTT vs payload (13, 128, 250 bytes), board vs emulator. The emulator
reads high by 0.4–1.2 quanta and cannot resolve payload steps smaller than a quantum.

Next, with the boards apart: PDR vs distance with firmware A and B. Tune `path_loss_exponent`,
`tx_power_dbm`, `noise_figure_db` to the measured curve and report the residual.

Success: PDR curves overlap within measurement variance on one nontrivial multi-node scenario,
and the latency gap is explained by the quantum.
