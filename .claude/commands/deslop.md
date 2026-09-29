---
description: Audit a file or directory for unnecessary code and delete it
---

Audit $ARGUMENTS for slop. Do not add anything. Your only output is deletions and
simplifications.

Work through these in order and report findings before changing anything:

1. **Dead code.** Every function, class, method, config field, parameter, and constant
   with no caller in the repo. Grep to confirm; do not guess. List each with its line
   count.

2. **One-implementation abstractions.** Interfaces, base classes, strategy patterns, and
   "pluggable" seams with exactly one concrete implementation and no second one planned
   in the next milestone. Propose collapsing each into the caller.

3. **Comment noise.** Comments that restate the code below them. Docstrings longer than
   one line that add no information the signature lacks. Section-header comments in files
   under 100 lines.

4. **Defensive scaffolding.** try/except that only re-raises. Validation of values that
   came from inside the process. Null guards for conditions the type system or the call
   graph already rules out.

5. **Retained legacy.** Anything kept "for debugging," "for compatibility," or
   commented out.

6. **Over-parameterization.** Functions with parameters that every caller passes the same
   value for. Config fields no scenario file sets.

For each finding, give the file, the line range, and one sentence on why it goes. Then
show me the list and wait for confirmation before deleting.

After deleting, run the test suite. If a test fails only because its subject is gone,
delete the test too. Report the net line count change.
