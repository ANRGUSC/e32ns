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

## Status snapshot (honest, post-correction — see D-019, D-020, D-021)

- **Built and unit-tested (software only):** the wireless medium, barrier orchestration,
  wire protocol, tracing, config, CLI — 54 tests, exercised against `MockNode`, a Python
  stand-in. **No ESP-IDF firmware has run.** This is a medium/orchestration harness, not
  milestone completion.
- **Written but UNVERIFIED:** `firmware/espnet_shim` (never compiled), `qemu_launcher.py`
  (never run). These are the three risk items (barrier/transport/linkage) — untested.
- **M1 DONE (real firmware):** ESP-IDF v5.4.2 `hello_world` booted on emulated `esp32s3` under
  Espressif QEMU 9.0.0 in WSL2; genuine UART captured (`docs/m1_qemu_boot.log`, D-020/V-002).
- **Spike #3 (shim linkage) DONE:** `--wrap` interposes the app's `esp_now_*` to our shim,
  confirmed on-target (D-021/V-003).
- **Spike #2 (transport) DONE:** real firmware's HELLO + ESP-NOW TX decoded by the host medium
  codec over open_eth+NAT (V-004, `docs/spike2_evidence.log`). Node app committed at
  `firmware/examples/espnet_node/`.
- **Binary wire body DONE (V-005):** C shim + Python emit/parse binary; proven on-target.
- **Spike #1 barrier — 2 of 3 axes validated:** clock-freeze CONFIRMED (V-006), throughput PASS
  (V-007: 421 slices/s @1 ms). Remaining: latency fidelity (two-node barrier↔medium integration).
- **Next:** two-node latency-fidelity measurement (finish D-023) → rerun `two_node.yaml` with one
  QEMU node + one MockNode = first *honest* M2.
- **V-001:** self-consistency units check (inputs determine the ~200 m output), *not* hardware
  validation.

---


---

# PART II — Consolidated Reference (folded-in docs)

> These sections were previously standalone files under `docs/`; folded here so the
> research log is the single source of truth. Raw evidence logs remain as data files
> (`docs/m1_qemu_boot.log`, `docs/spike2_evidence.log`). Cross-references to `../RESEARCH_LOG.md`
> inside these sections now point to this same document.


## Appendix A — Architecture

# Architecture

This expands the README diagram. Rationale for each choice is in `RESEARCH_LOG.md`
(cited as `D-NNN`).

## Components

### 1. Virtual nodes (`nodes/`, `firmware/`)
- **MockNode** (`nodes/mock_node.py`): pure-Python node for tests/demos (D-015).
- **QEMU + espnet_shim** (`firmware/`, `orchestrator/qemu_launcher.py`): real ESP-IDF
  firmware in QEMU; the shim overrides `esp_now_*` and pipes frames to the medium
  (D-002, D-017). Source-only in this repo (D-000).

Both speak one protocol (`docs/PROTOCOL.md`), so the medium is agnostic to node kind.

### 2. Transport (`transport/server.py`)
Asyncio TCP server; the medium is the long-lived listener, nodes are clients. Length-
prefixed JSON framing (D-003). Swappable for UNIX sockets / UART / `open_eth` later.

### 3. Simulation / barrier clock (`orchestrator/simulation.py`)
SliceTime-style barrier (D-004, D-014). Per slice: snapshot participants → `RUN_SLICE`
→ collect `TX` → wait all `SLICE_DONE` → `MediumEngine.process_batch` → dispatch
`TX_STATUS`/`RX`. Guarantees virtual-time skew between nodes ≤ one slice.

### 4. Medium engine (`medium.py`)
Owns node positions/peers/channels. Reproduces ESP-NOW semantics (D-012): send-callback
verdict (broadcast-unacked-success / unicast-ACK-or-fail with retries; fail causes
peer-not-exist / channel-mismatch / lost-on-air), RX dispatch, encryption policy. Uses:

### 5. Channel physics (`channel/`)
- `pathloss.py` — log-distance + shadowing, ns-3 defaults (D-006).
- `error_model.py` — `constant` | `range` (UDGM) | `snr_per` (YANS-style) (D-007).
- `airtime.py` — 802.11 DCF airtime (DIFS/SIFS/backoff/PLCP) (D-008).
- Collision: SINR vs summed interference, capture threshold (D-009, D-013).
All pure, unit-tested; one seeded RNG for determinism (D-011).

### 6. Config (`config/`) & tracing (`trace/`)
YAML scenarios with fail-fast validation; pcapng + radiotap traces stamped with
simulation-clock time and per-packet medium annotations (D-016).

### 7. CLI (`cli.py`)
`run` / `validate` / `info`. Entry point `esp-netsim`.

## Data flow of one ESP-NOW send

1. App calls `esp_now_send(peer, data)` → shim/MockNode emits `TX` at virtual time `t`.
2. Server buffers it into the current slice; node acks `SLICE_DONE`.
3. At the barrier, `MediumEngine.process_batch` computes airtime, interference/SINR,
   PER, and the send verdict; produces `TxOutcome` (status + per-receiver verdicts).
4. Simulation sends `TX_STATUS` to the sender (→ app send-cb) and `RX` to each
   receiver that passed (→ app recv-cb). TraceRecorder writes every attempt to pcapng.

## Determinism contract

A run is a pure function of `(scenario, seed)` given fixed node I/O order; the barrier
fixes the order, and all randomness flows through one seeded RNG (D-011). Same inputs →
byte-identical traces. This is the "repeatability of a simulator" the project targets.

## Extension points (designed-for, not yet built)

- New propagation/error models: implement the `PropagationLoss` / `ErrorRateModel`
  protocol (e.g. Nakagami fading, ANRG `gfge` Gilbert-Elliott — D-007).
- New transports: implement the server/− client framing over UNIX/UART/`open_eth`.
- Multi-channel adjacent-interference, per-symbol chunked SINR (D-007/D-013 futures).
- Hierarchical/sharded barrier for hundreds of nodes (D-004/D-011 futures).
- M6 GUI: consume the outcome-hook stream + pcapng; reuse ANRG ncsim web viz (brief §10).


## Appendix B — Node⇄Medium Wire Protocol

# Node ⇄ Medium Wire Protocol (v1)

This is the single contract between a virtual node (QEMU + `espnet_shim`, **or** the
pure-Python `mock_node`) and the host-side wireless-link medium. See decisions **D-002**
(why a protocol), **D-003** (transport), **D-005** (time unit) in `RESEARCH_LOG.md`.

## Transport & framing

- Transport: **TCP** to the medium server on `127.0.0.1:<port>` (default `52333`). One
  connection per node.
- Framing: every message is `uint32 big-endian length` + that many bytes of **binary body**
  (D-019 — JSON was a smell on the emulated-Xtensa hot path; the C shim now writes raw bytes).
  - Length-prefix (not newline-delimited) because 802.11/ESP-NOW payloads are arbitrary bytes.
  - The body is compact big-endian: `uint8 tag` then fixed fields; MACs are 6 raw bytes; a
    payload is `uint16 length + bytes`; floats are IEEE-754 `float32`. See the exact field
    layout in `src/esp_netsim/protocol.py` (`encode_body`/`decode_body`) — the authoritative
    reference the C shim mirrors (`firmware/components/espnet_shim/espnet_shim.c`), locked by
    `tests/test_protocol.py::test_binary_wire_layout_is_locked`.
  - A JSON form (`Message.to_json_bytes`) is retained for debugging only; it is not on the wire.

## Message envelope

Logical shape (as `Message.type` + payload; the wire form is the binary body above):

```
tag=<MSG_TYPE>  node=<u16>  t=<u64 micros>  ...type-specific fields...
```

- `type`/`tag` — message kind (table below; tag values in `protocol._TAG`).
- `node` — the sender/owner node id (the medium also knows it from the connection).
- `t` — virtual time in **integer microseconds** (D-005). For a QEMU node this is
  `esp_timer_get_time()` which, under the QMP barrier, equals virtual time because the guest
  clock freezes while paused (D-022). The medium sets delivery times ≥ the frame's `t` (D-014).

## Message types

### Node → Medium

| type        | fields                                                                                  | meaning |
|-------------|-----------------------------------------------------------------------------------------|---------|
| `HELLO`     | `node`, `caps` (obj: `{"espnow":true,"wifi":false}`), `mac` (str `"aa:bb:.."`)          | register on connect |
| `ADD_PEER`  | `node`, `peer` (mac str), `channel` (int), `encrypt` (bool), `lmk` (hex or null)        | mirrors `esp_now_add_peer` (brief §3) |
| `DEL_PEER`  | `node`, `peer`                                                                            | mirrors `esp_now_del_peer` |
| `TX`        | `node`, `t`, `src` (mac), `dst` (mac), `channel`, `rate_mbps`, `seq` (int), `data` (hex) | an ESP-NOW frame offered to the air |
| `SLICE_DONE`| `node`, `t`                                                                              | node has run to the slice boundary and is paused (barrier ack, D-004) |

### Medium → Node

| type        | fields                                                                                   | meaning |
|-------------|------------------------------------------------------------------------------------------|---------|
| `WELCOME`   | `node`, `slice_us` (int), `mac`                                                            | ack HELLO; announce slice size |
| `TX_STATUS` | `node`, `seq`, `status` (`"success"`\|`"fail"`), `t`                                       | the ESP-NOW **send callback** verdict (brief §3 — the single most important behavior) |
| `RX`        | `node`, `t`, `src` (mac), `dst` (mac), `channel`, `rate_mbps`, `rssi_dbm` (float), `data` (hex) | deliver a frame to this node's RX callback |
| `RUN_SLICE` | `until_t` (int)                                                                            | release the node to run until virtual time `until_t`, then pause and send `SLICE_DONE` |
| `SHUTDOWN`  | —                                                                                         | orderly stop |

## Semantics the medium guarantees

1. **Send-callback fidelity (brief §3).** For every `TX`, exactly one `TX_STATUS` is returned.
   - Unicast: `success` iff the frame was delivered *and* MAC-ACKed within the retry budget;
     `fail` otherwise (peer absent, wrong channel, or lost on air).
   - Broadcast (`ff:ff:ff:ff:ff:ff`): `success` as soon as it is transmitted (unacknowledged),
     regardless of who receives it.
2. **RX dispatch.** A node receives an `RX` for a frame iff it is in range with sufficient SINR
   on the same channel (and, for encrypted unicast, has the peer/LMK). Broadcast needs no peer.
3. **Virtual-time causality.** `RX`/`TX_STATUS` carry the *delivery* virtual time
   (`emit_t + airtime + propagation`), never earlier than the frame's `t`. During barrier mode
   all such events for a slice are computed while nodes are paused, then dispatched (D-004).

## Why binary (D-019)

The original v1 used JSON+hex for debuggability. That was changed to binary **before** the
barrier-timing spike: hand-encoding JSON in C on an emulated Xtensa core, in the exact path we
are about to time, adds cost and a variable to the measurement (reviewer). The binary body is
trivial to write in C (fixed-layout big-endian, no allocator, no string formatting), removes the
hex-doubling of payloads, and keeps the same `Message` semantics — the medium/orchestrator code
is unchanged. Proven end-to-end: the C shim's binary HELLO/TX are decoded by the host codec with
`t` carrying `esp_timer` micros (`docs/spike2_evidence.log`). A JSON debug form remains available
off-wire.


## Appendix C — Milestone Status

# Milestone status (honest — see RESEARCH_LOG.md D-019)

Milestones from the project brief (PDF) and research brief §9. **No milestone is "complete":
no ESP-IDF firmware has executed yet.** What exists is the host-side medium/orchestration,
unit-tested against `MockNode` (a Python stand-in), plus *unverified* QEMU-path source.
The critical correction (D-019): the earlier "Done (mock)" labels over-claimed — the milestones
are only meaningful with real firmware underneath, which is the work now in progress.

| M | Goal | Honest status | Notes |
|---|------|---------------|-------|
| **M1** | Single ESP32-S3 boots under QEMU, UART captured | **DONE (real firmware)** | ESP-IDF v5.4.2 hello_world booted on emulated esp32s3, QEMU 9.0.0/WSL2; UART in `docs/m1_qemu_boot.log`; repro `firmware/qemu/*.sh` (D-020/V-002) |
| **M2** | Two-node link, controlled delivery/loss | **DONE (real firmware)** | real QEMU node (QMP-gated) + MockNode on one barrier; 140 ESP-NOW frames delivered through the medium, RSSI from path-loss model (V-009, `firmware/qemu/m2_run.py`) |
| **M3** | Multi-node ESP-NOW + traces | **Medium layer only** | path-loss/PER/collision + pcapng tested vs `MockNode`; NOT with firmware |
| **M4** | Wi-Fi Internet gateway via host NAT | **Designed, unverified** | `open_eth`+NAT plan in `QEMU_INTEGRATION.md` |
| **M5** | Experiment framework: config → launch → collect | **Mock launcher only** | launches `MockNode`s; QEMU launch path unverified |
| **M6** | GUI | **Not started** | outcome-hook + pcapng streams available to build on |

## Risk-first order (brief §9) — live status

1. **Shim linkage (risk #3): ✅ RESOLVED (D-021/V-003).** Empirically: naive component symbol
   provision does NOT interpose (link order favors IDF; the app called the real `esp_now_init`
   and crashed). `--wrap` IS required and WORKS — confirmed in QEMU: the app calls our
   `__wrap_esp_now_*` with zero app-source edits. Enabled by default in the component.
2. **Transport (risk #2): ✅ RESOLVED (V-004).** Real firmware brings up `open_eth`+DHCP
   (`GOT_IP 10.0.2.15`), the `--wrap`'d `esp_now_send` pipes a frame out of QEMU via slirp NAT,
   and the **real `esp_netsim` codec decoded the HELLO + ESP-NOW TX** on the host
   (`docs/spike2_evidence.log`). Proven node app: `firmware/examples/espnet_node/`.
   *Remaining:* swap JSON body → length-prefixed binary *before* timing per-frame overhead.
3. **Barrier timing (risk #1): 🟡 TWO OF THREE AXES VALIDATED.**
   - **Clock freeze — CONFIRMED (V-006):** guest `esp_timer` freezes on QMP `stop` (guest
     advanced 0.22 s across a 3.27 s wall pause), so `esp_timer` == virtual time (D-022 holds).
   - **Throughput — PASS (V-007/D-024):** QMP `stop`/`cont` over a UNIX socket costs ~0.75–1.4 ms/
     cycle → 421 slices/s at a 1 ms slice, 919/s at 250 µs — ≥100/s bar cleared 4×. (An early
     88 ms/11-per-s reading was a TCP delayed-ACK artifact, ruled out.)
   - **Latency fidelity — MEASURED (V-008):** real node under the barrier, 300 frames.
     1 ms slice → 52.5% error (FAIL); **250 µs slice → 14.3% error (PASS)**. A 250 µs slice
     passes both fidelity and throughput (919/s). Trace timestamps are exact at any slice;
     deterministic offline mode gives zero quantization if needed.
   **Risk #1 RETIRED** — clock-freeze holds, throughput passes, fidelity passes at 250 µs, offline
   mode as exact-fidelity fallback. No redesign forced.

**Then:** rerun `two_node.yaml` with one QEMU node + one MockNode = the first *honest* M2.

## What the software layer proves and does not

**Proves:** the medium math (path loss, PER, airtime, collision) and ESP-NOW send/recv
*semantics* are implemented and internally consistent, deterministic, and unit-tested.

**Does NOT prove:** that real ESP-IDF firmware links against the shim, that frames actually
leave QEMU, that the barrier holds real timing, or that any of it matches hardware. Those are
the unverified assumptions the risk spikes exist to test.

## Recommended next steps (in order)

1. Bring up M1 on Linux + Espressif QEMU fork; boot `firmware/examples/espnow_ping`.
2. Spike the three risks (barrier timing, transport, shim linkage) — `QEMU_INTEGRATION.md`.
3. Wire `QemuNodeHandle` as a barrier gate in `Simulation` (small refactor: gate
   interface with `MockNode` = message-ack, `QEMU` = QMP stop/cont).
4. Hardware validation: ESP-NOW latency + PDR, virtual vs. real ESP32-S3 (brief §9 rec 3,
   `docs/VALIDATION.md`) — the centerpiece plot for a demo/tool paper (EWSN/IPSN/WNS3).
5. M6 GUI and ANRG PyWiSim/ncsim interoperation.


## Appendix D — QEMU + ESP-IDF Integration Runbook

# QEMU + ESP-IDF Integration Runbook (M1, M4)

> **Status:** the code in `firmware/` and `orchestrator/qemu_launcher.py` is delivered
> as source. It is **not** exercised in the Python repo's CI because this project was
> developed on Windows without `qemu-system-xtensa` or ESP-IDF (research-log D-000).
> This runbook is the step-by-step to bring it up on a Linux host, and the three
> highest-risk spikes to do first (brief §9).

## Why the medium code doesn't change

A QEMU node and a `MockNode` are interchangeable clients of the same wire protocol
(`docs/PROTOCOL.md`, decisions D-002/D-015). The medium, barrier clock, tracing, and
config are identical. Only two things are QEMU-specific:

1. **Transport:** the guest's `espnet_shim` connects a TCP socket (over the emulated
   `open_eth` NIC + user-mode NAT) to the host medium at `10.0.2.2:52333`.
2. **Barrier gate:** instead of acking `RUN_SLICE` messages, a QEMU node is paused
   and resumed by the clock master via QMP `stop`/`cont` (`qemu_launcher.QmpClient`).

## Prerequisites (Linux)

```bash
# 1. Espressif QEMU fork (has the esp32s3 machine; upstream QEMU does not).
#    See github.com/espressif/qemu releases, or:
#    idf.py install-qemu  (recent ESP-IDF)  OR  build the fork from source.
qemu-system-xtensa --version           # must be the espressif fork

# 2. ESP-IDF (pin a version! ESP-NOW payload limits are version-sensitive — brief §10c,f).
#    v5.4.2+ if you want ESP-NOW v2.0 (1470-byte) payloads.
. $IDF_PATH/export.sh
```

## Build the example firmware

```bash
cd firmware/examples/espnow_ping
idf.py set-target esp32s3
idf.py -DCONFIG_ESPNET_EMULATION=1 -DCONFIG_ESPNET_NODE_ID=1 build
# Produce a single flash image QEMU can boot:
esptool.py --chip esp32s3 merge_bin -o build/flash.bin @build/flash_args
```

## Run a single node under QEMU (M1)

```bash
# Start the host medium first (it is the long-lived TCP listener, D-003):
esp-netsim run configs/two_node.yaml     # or a dedicated qemu scenario

# Then boot the node (qemu_launcher builds this argv for you):
qemu-system-xtensa -nographic -machine esp32s3 \
  -drive file=build/flash.bin,if=mtd,format=raw \
  -nic user,model=open_eth,guestfwd=tcp:10.0.2.2:52333-tcp:127.0.0.1:52333 \
  -qmp unix:/tmp/espnet-qmp-1.sock,server=on,wait=off -S
```

The Python side drives launch + gating via:

```python
from esp_netsim.orchestrator.qemu_launcher import QemuNodeSpec, QemuNodeHandle
h = QemuNodeHandle(QemuNodeSpec(node_id=1, flash_image="build/flash.bin",
                                medium_port=52333))
await h.start()          # boots paused (-S)
await h.run_slice(0.001) # cont, wait one slice, stop  (QMP barrier, D-004)
```

## Spike these THREE unknowns first (brief §9)

1. **Barrier timing fidelity (highest risk).** Two QEMU nodes, ESP-NOW ping; measure
   one-way latency under the QMP `stop`/`cont` barrier and compare to the ~1–2 ms real
   MAC latency (brief §3). If the barrier can't hold it without collapsing throughput,
   switch to the deterministic offline mode (run-to-barrier, no real-time pacing) or
   investigate `-icount` on the fork (brief thresholds, D-004/D-014).
2. **Transport out of QEMU.** Compare `open_eth`-socket vs a dedicated UART/USB-serial-
   JTAG channel for per-frame overhead (brief §9 risk #2). The `espnet_transport.h`
   abstraction lets you swap backends without touching the shim logic.
3. **Shim linkage.** Confirm the app links `espnet_shim`'s `esp_now_*` rather than
   IDF's. Try both: component symbol provision (default) and the `--wrap` variant in
   `firmware/components/espnet_shim/CMakeLists.txt` (brief §9 risk #3).

## Wi-Fi/Internet gateway (M4)

For infrastructure Wi-Fi to the real Internet, keep `open_eth` + user-mode NAT and let
lwIP do real DNS/TCP/UDP/HTTP through the host (brief §1, §9 M4):
`-nic user,model=open_eth,hostfwd=tcp:127.0.0.1:8080-:80`. A virtual-AP abstraction
node terminates association and forwards; ESP-NOW frames still go through the medium.

## Known caveats (from the brief; verify during the spike)

- The exact `hw/xtensa/esp32s3.c` path and `open_eth` base/IRQ were *inferred* in the
  brief; confirm by grepping the espressif/qemu tree (brief caveats).
- `-netdev socket` TCP mode has reconnect fragility; we make the medium the stable
  listener and the node the reconnecting client to avoid it (D-003, brief caveats).
- ESP-NOW real-hardware latency figures are third-party/field-study numbers, not an
  Espressif spec; treat the MicroPython S3 11–31 ms figure as Python-stack overhead,
  not raw MAC (brief §3 caveats).


## Appendix E — Validation Plan

# Validation plan

The centerpiece of a tool/demo paper is showing the emulator reproduces real-hardware
behavior on a nontrivial scenario (brief §8, §9 rec 3). This documents what is validated
now and the hardware-validation protocol to run next.

## Done now: model self-consistency (no hardware)

- **V-001 (range).** Distance sweep of the `snr_per` model yields ~200 m effective LOS
  range, matching published ESP-NOW LOS range 190–250 m (brief §3). See `RESEARCH_LOG.md`
  V-001. This is a *self-consistency* check, not hardware validation.
- **Unit-level reference points.** Friis reference loss = 40.046 dB (vs ns-3 40.0459);
  DCF constants slot/SIFS/DIFS = 20/10/50 µs; airtime order-of-magnitude ~1 ms for small
  ESP-NOW frames (vs measured 1059 µs @1 B). See `tests/test_channel_models.py`.

## Next: hardware validation (needs ESP32-S3 boards + Linux QEMU)

Reproduce two headline metrics from the literature and match them virtual-vs-real:

1. **One-way ESP-NOW latency vs payload.** Real target: ~1059 µs @1 B, ~1869 µs @100 B
   on ESP32-CAM (WONS 2025, brief §3). Run the same firmware on N real boards and N QEMU
   nodes; compare latency distributions (median + P95/P99 tail).
2. **Packet-delivery ratio vs distance.** Real target: PDR degrades with distance with
   heavy-tailed delay (brief §3); optionally fit against ANRG's 100-node PRR datasets
   (anrg.usc.edu/downloads, brief §10). Tune `path_loss_exponent`, `shadowing_sigma_db`,
   `noise_figure_db`, `tx_power_dbm` to the measured curve.

### Protocol

- Same ESP-IDF app both places (`firmware/examples/espnow_ping`, unmodified).
- Timestamp with the app's monotonic clock on hardware; use simulation-clock time in the
  emulator (traces already carry it, D-016).
- Report the tuned medium parameters and the residual error; enable shadowing to capture
  the heavy tail.

### Success criterion

Latency medians within a small factor and PDR-vs-distance curves overlapping within the
measurement's own variance on at least one nontrivial multi-node scenario. That plot is
the demo/tool-paper centerpiece (target EWSN/IPSN demo or WNS3, brief §8).

## Threshold that changes the plan (brief §9 risk #1)

If the QMP barrier can't hold two-node latency within a few hundred µs of the ~1 ms real
MAC without collapsing throughput, switch to deterministic offline mode (run-to-barrier,
no real-time pacing) or investigate `-icount` on the Xtensa fork (D-004/D-014).


## Appendix F — Risk-Retirement Status Update

# ESP32-S3 Network Emulator — Risk-Retirement Update

**One line:** the three highest-risk unknowns of the QEMU-based ESP-NOW emulator are now retired
with on-target measurements, the emulator boots real firmware, and a real ESP-NOW frame has
travelled from emulated firmware into the host medium — no redesign forced.

This update covers the feasibility phase (the research brief's §9 "spike these first" list). All
claims below are backed by artifacts in this repo; every decision is in `RESEARCH_LOG.md`.

---

## What is now proven (with evidence)

| # | Result | Evidence |
|---|--------|----------|
| **M1** | Real ESP-IDF v5.4.2 `hello_world` boots on emulated ESP32-S3 (Espressif QEMU 9.0.0) | `docs/m1_qemu_boot.log`; repro `firmware/qemu/setup_esp_idf.sh` + `run_qemu.sh` (D-020) |
| **Risk #3 — shim linkage** | Component symbol provision does **not** interpose (link order favors IDF; the app called the real `esp_now_init` and crashed). **`--wrap` is required and works** — app calls our shim with zero source edits | `RESEARCH_LOG.md` D-021 |
| **Risk #2 — transport** | A real ESP-NOW broadcast leaves QEMU (shim → open_eth → slirp NAT) and is decoded by the host medium's real codec: `RECV HELLO`, `RECV TX` | `docs/spike2_evidence.log`; `firmware/examples/espnet_node/` |
| **Wire format** | Swapped JSON → length-prefixed **binary** (C shim + Python), proven on-target; `t` carries `esp_timer` micros | D-019/V-005; layout locked by a test |
| **Risk #1 — barrier, clock freeze** | Guest `esp_timer` **freezes** on QMP `stop` (advanced 0.22 s across a 3.27 s wall pause) → `esp_timer` == virtual time, no offset model needed | `firmware/qemu/clock_freeze_probe.py`, V-006 |
| **Risk #1 — barrier, throughput** | QMP `stop`/`cont` over a **UNIX socket** costs ~0.75 ms/cycle → **421 slices/s @ 1 ms**, 919/s @ 250 µs (≥100/s bar cleared) | `firmware/qemu/barrier_overhead_probe.py`, V-007 |
| **Risk #1 — barrier, latency fidelity** | Real node, 300 frames: **1 ms slice → 52.5 % error (fail); 250 µs slice → 14.3 % (pass)** | `firmware/qemu/barrier_latency_probe.py`, V-008 |

**Bottom line:** the driver-layer-shim + host-medium + QMP-barrier architecture (research brief's
recommendation) is **viable**. Retiring these three risks is what the feasibility phase was for.

---

## Key design conclusions (measured, not assumed)

1. **Use `--wrap` for the shim, always.** Providing `esp_now_*` symbols from a component looks
   like it links but silently loses to IDF's archive. `--wrap` is mandatory (D-021).
2. **QMP must ride a UNIX socket, never TCP.** TCP QMP measured **88 ms/cycle** (delayed-ACK
   artifact); UNIX socket is **~0.75 ms** — a 100× difference that would otherwise be invisible.
3. **`esp_timer` == virtual time** under the barrier, because the guest clock freezes on `stop`.
   This is the cleanest possible time model — no drift/offset tracking (D-022, V-006).
4. **Default the barrier slice to 250 µs for ESP-NOW latency experiments** (not 1 ms). At 250 µs,
   both throughput (919/s) and latency fidelity (14.3 %) pass; 1 ms fails fidelity because the
   barrier overhead is the same order as the ~0.9 ms MAC latency. For experiments needing exact
   timing, **deterministic offline mode** removes quantization entirely (trace timestamps are
   already exact at any slice).

---

## Honest limitations (what is NOT yet done)

- **No full multi-node run yet.** M1 + the three spikes are done; the medium/orchestration layer
  is unit-tested against a Python stand-in (`MockNode`, 56 tests) but has not yet driven two real
  QEMU nodes through a shared barrier. That integration is the immediate next step.
- **The latency number is a one-hop lower bound.** It measures the medium's dispatch quantization
  (sender→dispatch); a full sender→receiver→app round trip adds the receiver's slice. 14.3 % at
  250 µs could grow two-hop; offline mode is the exact-fidelity answer.
- **No hardware validation.** All numbers are QEMU + host. The tool-paper centerpiece — virtual
  vs. real ESP32-S3 ESP-NOW latency/PDR — still requires physical boards (`docs/VALIDATION.md`).
- **Model self-consistency only:** the ~200 m emergent range matching published ESP-NOW range
  (V-001) is a units check, not validation.

---

## Recommended next steps

1. **First honest M2:** wire `QemuNodeHandle`'s QMP gate into the clock master and run
   `two_node.yaml` with one real QEMU node + one MockNode through the shared barrier.
2. Two real QEMU nodes; confirm end-to-end reactive latency two-hop at 250 µs / offline.
3. Hardware validation plot (virtual vs. real), then M4 (Wi-Fi gateway) and M6 (GUI).

---

*All decisions and the full derivation are in [`RESEARCH_LOG.md`](../RESEARCH_LOG.md)
(D-000 … D-025, V-001 … V-008). Milestone-by-milestone status in
[`MILESTONES.md`](MILESTONES.md).*
