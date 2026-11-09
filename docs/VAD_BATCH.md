# VAD UNIT batch discovery — staged, opt-in

Status: watcher, batch and staging-only launchd config generator are available on draft PR #16 (stacked on PR #5). The watcher is **not installed or running**, and no real HDD UNIT has been processed. The owner confirmed a disposable INT-5 FCPXML bundle imports into DaVinci Resolve.

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

Default for the batch CLI: dry run, no editor invocation and no creation. `--run` explicitly opts in, and requires a pre-existing local editor script. The **separate** `vad_watch.py` has polling logic but has not been enabled on the owner's Mac; simply copying files does not yet trigger processing.

## Durability and replay

- The wrapper waits for input files to be nonempty and at least 60 seconds old, compares the source identity before/after execution, and refuses symlinked files. Source identity uses relative path, size, nanosecond modification time, inode and SHA-256 of the first/last 64 KiB of each file. ctime is excluded because a read of a Mac fixture changed it without changing content. This is a **bounded content sample**, not a full media content hash or a guarantee against all concurrent changes.
- Each output is generated at `.OUTPUTNAME.staging/OUTPUTNAME` in the same INT/NAR directory. Staging is retained on failure or a change to input; a later run reports `BLOCKED_PARTIAL`, never silently resumes or overwrites it.
- A successful output is published by a same-filesystem rename, followed by an adjacent `OUTPUTNAME.manifest.json`. The manifest binds the unit path, source signature, output member metadata and bounded-content signature and engine. A verified repeat reports `SKIP_VERIFIED`.
- An old output without a matching manifest, a changed input/output, or a crash after output rename but before manifest creation reports `BLOCKED_UNVERIFIED`. Such outputs are **not** overwritten. Inspect them locally before any manual recovery.
- The output validation requires nonempty regular output members and rejects symlinks, but does not independently verify DaVinci Resolve compatibility; the owner separately confirmed import of a disposable INT-5 bundle, but no full watcher-created live HDD bundle has been imported.
- Failures/partial stages need operator inspection. Do not automatically delete them; media processing is not assumed idempotent. No batch-retry loop is enabled.

## Local polling supervisor

`tools/vad_watch.py` is opt-in. It does not install or enable a launchd job. It checks that the exact external volume is mounted and the root is beneath it, then scans every two minutes. A UNIT needs two identical scans separated by at least two minutes, with every file at least two minutes old. It processes one UNIT per scan. A second supervisor exits with `ALREADY_RUNNING` because of a local file lock. Outputs remain subject to the batch allowlist; the supervisor now stops on blocked/unverified existing outputs or failed unit processing, rather than advancing to other READY units. A JSON line log lives in the local state directory. If the volume disappears, changes path, or has less than 5 GiB or twice the source size free, the supervisor logs `BLOCKED` and exits. Mac sleep pauses polling; it resumes after wake. This timing check cannot prove a copy is complete if a copier pauses for more than the quiet interval. For strict completion proof, a copier must add an explicit DONE marker after copying, which is not part of this version.

Example after fixture acceptance and explicit approval to run against the HDD:

```sh
python3 tools/vad_watch.py --volume "/Volumes/New Volume" --root "/Volumes/New Volume" --editor "$HOME/Downloads/youtube_edit.py" --state-dir "$HOME/Library/Application Support/youtube-vad"
```

Use `--once` for a read-only mount/discovery check. To stop a running supervisor, create `STOP` in its state directory; it exits on the next poll. Remove that marker before a later start. Review `vad_watch.jsonl` for `CREATED`, `SKIP_VERIFIED`, `BLOCKED_PARTIAL`, `BLOCKED_UNVERIFIED`, `FAILED`, or `BLOCKED`. Reports from the Mac editor go to `state-dir/reports`, not its default `~/Downloads/runs`. The supervisor does not automatically restart after a blocking error; a future launchd installation could restart it after the cause is fixed. No unattended operation is active until that installation is approved and performed.

## Next activation gates

The existing Mac editor generated an INT-5 disposable fixture with timecode and verified manifest. Its report says REVIEW because the test tone had no speech. The owner confirmed that this FCPXML imports into DaVinci Resolve. Mac `--once` read-only discovery on `/Volumes/New Volume` logged `SCAN_ONLY` and `jobs: 15`, but this counts discovered units, **not necessarily READY units**. The Mac copy in `~/Documents/Codex/.../work/youtube-automation` is not a git checkout and has **not** been verified to match this PR head. Real unattended watcher E2E, live HDD processing, NAR output, reconnect/sleep behavior, and launchd activation remain unproved. Before full-root activation, inspect all discovered unit statuses: enabling an unattended watcher at the volume root could process existing READY units, not just future backups. Approve scope and launchd activation separately from merge.


## Staged launchd configuration (NO activation)

`tools/vad_launchd_prepare.py` generates a reviewable `com.exotictraveler.youtube-vad.plist` for macOS using the explicit watcher, editor, volume, root and Mac-local state-directory paths. This is **not** an installer: running without `--write` only prints the plan; `--write` creates a plist **only** under `STATE_DIR/staged/`, never under `~/Library/LaunchAgents`, and never calls `launchctl`. It refuses missing/non-mounted external volumes, incorrect paths and overwriting the staged plist. Unit/RAW content is not modified.

After pinning and copying the approved PR code into a stable Mac-local code directory (outside iCloud Desktop/Documents), a Mac operator can prepare the plist for review. **Do not run this against the temporary Codex Documents worktree or directly load it into launchd.** The plist uses the interpreter preparing it; PATH explicitly includes that interpreter and common Homebrew locations for ffmpeg and ffprobe. Run a disposable-only watcher acceptance test before production activation. Production `RunAtLoad` / `KeepAlive` configuration is inert until installed and bootstrapped with launchctl in a separately approved Mac operation; it may retry after a blocked mount/error and will stop if a STOP marker causes a successful exit. Validate launchd recovery on the actual Mac; it is not covered by GitHub's Linux CI.

For an audit of the exact command plan (no writes), an operator would use:

```sh
python3 tools/vad_launchd_prepare.py \
  --volume "/Volumes/New Volume" \
  --root "/Volumes/New Volume" \
  --editor "$HOME/Downloads/youtube_edit.py" \
  --watcher "$HOME/Library/Application Support/youtube-vad/code/vad_watch.py" \
  --state-dir "$HOME/Library/Application Support/youtube-vad"
```

Once all prerequisite approvals and real Mac checks are complete, `--write` only stages a local plist. It **does not** activate a service. No production launchd activation, full-HDD run, or PR merge is authorized by committing this template.
