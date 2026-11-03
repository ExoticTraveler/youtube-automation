# VAD UNIT batch discovery — staged, opt-in

Status: code and synthetic CI only. **Not installed as an automatic Mac watcher**, and no real HDD unit has been processed by this PR.

The existing Mac-local `youtube_edit.py` remains the editor. This wrapper only discovers units and invokes that editor when explicitly allowed. No video/audio or personal transcript is stored in GitHub.

## Layout and output policy

```text
RAW_ROOT/COUNTRY/CITY/INT/INT-1/<raw camera files>
RAW_ROOT/COUNTRY/CITY/NAR/NAR-1/<raw narration files>
                    ↓
OUTPUT_ROOT/COUNTRY/CITY/INT/INT-1_CLEAN_VAD.fcpxmld
OUTPUT_ROOT/COUNTRY/CITY/NAR/NAR-1_CLEAN_VAD.fcpxmld
```

If RAW_ROOT is already a country directory, its directory name supplies COUNTRY. The country and city values are discovered dynamically; do not use a city name as a UNIT. A UNIT must match `INT-<number>` or `NAR-<number>` under its matching kind folder. Two cities can each contain `INT-1`, with distinct outputs.

OUTPUT_ROOT must be explicitly provided and must be separate from RAW_ROOT. The wrapper rejects roots nested inside each other, symlinked units, and output paths that resolve into RAW. Never direct this script to write generated data into a RAW folder.

## Usage on the owner's Mac (example placeholders; not a production command)

```sh
python3 tools/vad_batch.py --raw-root "<RAW_ROOT>" --output-root "<OUTPUT_ROOT>"
# after inspecting the planned jobs and choosing a non-RAW output directory:
python3 tools/vad_batch.py --raw-root "<RAW_ROOT>" --output-root "<OUTPUT_ROOT>" --editor "<LOCAL_YOUTUBE_EDIT_PY>" --run
```

Default: dry run, no editor invocation and no creation. `--run` explicitly opts in, and requires a pre-existing local editor script. This has not yet been integrated with a file watcher or launchd; simply copying files does not trigger it automatically.

## Durability and replay

- The wrapper waits for input files to be nonempty and at least 60 seconds old, compares the source identity before/after execution, and refuses symlinked files. Source identity uses relative path, size, nanosecond modification/change time and inode. It is **not a full content hash** or a guarantee that a concurrently changing filesystem cannot evade detection.
- Each output is generated at `.OUTPUTNAME.staging/OUTPUTNAME` under OUTPUT_ROOT. Staging is retained on failure or a change to input; a later run reports `BLOCKED_PARTIAL`, never silently resumes or overwrites it.
- A successful output is published by a same-filesystem rename, followed by an adjacent `OUTPUTNAME.manifest.json`. The manifest binds the unit path, source signature, output member metadata signature and engine. A verified repeat reports `SKIP_VERIFIED`.
- An old output without a matching manifest, a changed input/output, or a crash after output rename but before manifest creation reports `BLOCKED_UNVERIFIED`. Such outputs are **not** overwritten. Inspect them locally before any manual recovery.
- The output validation requires nonempty regular output members and rejects symlinks, but does not independently verify that Final Cut can import the entire .fcpxmld bundle. That remains a Mac acceptance test.
- Failures/partial stages need operator inspection. Do not automatically delete them; media processing is not assumed idempotent. No batch-retry loop is enabled.

## Next activation gates

Run the existing Mac editor against synthetic/throwaway UNIT fixtures and verify the generated bundle in Final Cut. Confirm actual media stability and output path conventions; only then consider installing a local watcher with bounded polling and one active job. Mac authentication, PR merge and production rollout are separate decisions.
