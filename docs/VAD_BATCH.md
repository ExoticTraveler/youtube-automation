# VAD UNIT batch discovery — staged, opt-in

Status: code and synthetic CI only. **Not installed as an automatic Mac watcher**, and no real HDD unit has been processed by this PR.

The existing Mac-local `youtube_edit.py` remains the editor. This wrapper only discovers units and invokes that editor when explicitly allowed. No video/audio or personal transcript is stored in GitHub.

## Layout and output policy

```text
ROOT/COUNTRY/CITY/INT/INT-1/<original camera files>      (source: never written)
ROOT/COUNTRY/CITY/INT/INT-1_CLEAN_VAD.fcpxmld            (generated, sibling of UNIT)
ROOT/COUNTRY/CITY/INT/INT-1_CLEAN_VAD.fcpxmld.manifest.json
ROOT/COUNTRY/CITY/INT/.INT-1_CLEAN_VAD.fcpxmld.staging/  (only while running / after failure)
ROOT/COUNTRY/CITY/NAR/NAR-1/<original narration files>
ROOT/COUNTRY/CITY/NAR/NAR-1_CLEAN_VAD.fcpxmld
```

`--root` (alias `--raw-root`) is the single tree that holds both source UNITs and their generated outputs. There is no separate output root any more; `--output-root` is rejected. If ROOT is itself a country directory, `CITY/KIND/UNIT` is also accepted. Country and city are discovered dynamically; a UNIT must match `INT-<number>` or `NAR-<number>` under its matching kind folder. Two cities can each contain `INT-1`, with distinct outputs.

### Write allowlist (what "RAW protection" now means)

The wrapper writes to exactly three names per UNIT, all directly inside that UNIT's `INT`/`NAR` directory: `{UNIT}_CLEAN_VAD.fcpxmld`, its `.manifest.json`, and the hidden `.staging` directory. `validate_output_target` checks this before discovery reports a job and again immediately before writing:

- the UNIT is a canonical (non-symlink, fully resolved) directory inside ROOT at depth `[COUNTRY/]CITY/KIND/UNIT`, its kind folder matches its prefix, and no ancestor is itself a UNIT;
- the output path is **exactly** `UNIT.parent / (UNIT.name + "_CLEAN_VAD.fcpxmld")`. A matching filename elsewhere, `..` traversal, other kind/city folders, or any path inside a UNIT tree is refused (`BLOCKED_OUTPUT_PATH`);
- the INT/NAR directory is not a symlink and resolves to itself.

Nothing inside a UNIT directory is ever created, renamed or deleted. The editor reads `--unit` and writes only into the staging directory. Discovery does not follow symlinks, and it does not descend into hidden directories or `*.fcpxmld` bundles, so generated output is never mistaken for source.

## Usage on the owner's Mac (example placeholders; not a production command)

```sh
python3 tools/vad_batch.py --root "<ROOT>"
# after inspecting the planned jobs (dry run, writes nothing):
python3 tools/vad_batch.py --root "<ROOT>" --editor "<LOCAL_YOUTUBE_EDIT_PY>" --run
```

Default: dry run, no editor invocation and no creation. `--run` explicitly opts in, and requires a pre-existing local editor script. This has not yet been integrated with a file watcher or launchd; simply copying files does not trigger it automatically.

## Durability and replay

- The wrapper waits for input files to be nonempty and at least 60 seconds old, compares the source identity before/after execution, and refuses symlinked files. Source identity uses relative path, size, nanosecond modification/change time, inode and SHA-256 of the first/last 64 KiB of each file. This is a **bounded content sample**, not a full media content hash or a guarantee against all concurrent changes.
- Each output is generated at `.OUTPUTNAME.staging/OUTPUTNAME` in the same INT/NAR directory. Staging is retained on failure or a change to input; a later run reports `BLOCKED_PARTIAL`, never silently resumes or overwrites it.
- A successful output is published by a same-filesystem rename, followed by an adjacent `OUTPUTNAME.manifest.json`. The manifest binds the unit path, source signature, output member metadata and bounded-content signature and engine. A verified repeat reports `SKIP_VERIFIED`.
- An old output without a matching manifest, a changed input/output, or a crash after output rename but before manifest creation reports `BLOCKED_UNVERIFIED`. Such outputs are **not** overwritten. Inspect them locally before any manual recovery.
- The output validation requires nonempty regular output members and rejects symlinks, but does not independently verify that Final Cut can import the entire .fcpxmld bundle. That remains a Mac acceptance test.
- Failures/partial stages need operator inspection. Do not automatically delete them; media processing is not assumed idempotent. No batch-retry loop is enabled.

## Next activation gates

Run the existing Mac editor against synthetic/throwaway UNIT fixtures and verify the generated bundle in Final Cut. Confirm actual media stability and output path conventions; only then consider installing a local watcher with bounded polling and one active job. Mac authentication, PR merge and production rollout are separate decisions.
