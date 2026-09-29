# Working rules for this repo

Read this before writing code. These rules override your defaults.

## The one rule

Write the smallest correct thing that a reviewer could hold in their head. If you are
adding code because it "might be useful," stop. If you are adding a comment because the
code looks bare without one, stop.

## Hard constraints

**No speculative generality.** Do not write an interface, base class, plugin point, or
"pluggable backend" until there are two real callers. One implementation goes in a
function. If you catch yourself writing "so this can be swapped later," delete it.

**No unused code.** Every function, class, config field, and parameter must have a caller
in the repo or a test that exercises it. If you write something no one calls, delete it
before you finish the turn. Do not leave it "for later."

**Docstrings: one line.** State what the function does. Nothing else. No Context /
Rationale / Consequences sections, no restating the signature, no citing D-numbers.
Rationale belongs in RESEARCH_LOG.md, which exists precisely so it does not belong here.

**Comments explain why, never what.** `# convert to dBm` above a dBm conversion is noise.
`# slirp maps the host to 10.0.2.2` earns its place. If the code needs a comment to be
readable, first try renaming things.

**One D-number citation per file, maximum**, at the top, only when a whole file
implements one decision. Not on every function.

**No defensive scaffolding.** No try/except that re-raises. No validation of internal
callers. No `if not x: return` guards for conditions that cannot occur. Validate at the
process boundary (config load, wire decode) and trust yourself afterward.

**Delete instead of deprecating.** No `to_json_bytes` kept "for debugging" after the wire
went binary. No commented-out code. Git remembers.

## Size budgets

Treat these as smells, not laws. Crossing one means stop and reconsider the design.

- Function over 40 lines
- File over 300 lines
- More than 3 parameters (use a dataclass)
- More than 2 levels of nesting
- A new file when an existing one has room

## Python specifics

- Stdlib first. A new dependency needs a one-line justification in the PR.
- Dataclasses over dicts-with-known-keys. Plain functions over classes with one method.
- No `Optional[X] = None` parameters that are always passed.
- No module-level lookup dicts that are used once. Inline it.
- Type hints on public functions. Not on locals.

## C specifics (firmware/)

- No hand-rolled parsers or encoders when a fixed-layout struct works.
- No abstraction layer over a single transport.
- Fixed-size buffers with explicit bounds. No dynamic allocation in the shim.

## Tests

Test behavior at the seams: the wire codec, the medium's outputs, the barrier's ordering.
Do not test getters, do not test that a dataclass stores its fields, do not add a test
purely to raise the count. One good test beats five shallow ones. Delete tests whose
subject was deleted.

## Docs

RESEARCH_LOG.md is the single source of truth for decisions. README.md is a front door:
what this is, how to run it, where to look. Do not create a third document. Do not create
a per-feature doc. If you want to explain something, explain it in the log.

## When finishing a turn

Before you report done, reread your own diff and answer:

1. What in this diff has no caller?
2. What comment restates the line below it?
3. What abstraction has exactly one implementation?
4. What would a reviewer ask me to delete?

Then delete those things. Report what you deleted alongside what you added.

## Tone in reports

State what happened. Do not narrate effort, do not add emoji status tables, do not use
bold for emphasis on every third phrase. If a result is uncertain, say so plainly.
