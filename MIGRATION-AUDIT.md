# Ente Photos v0.4.5: migration audit (read-only)

This is **only** the initial migration audit. It does **not** download media,
change `~/Pictures/Camera`, update the old SQLite database, or install or
activate any systemd units. The existing export-based sync remains unchanged.
An online Ente CLI listing can update the CLI's own metadata cache.

## Requirements

- Python 3.10+ (standard library only)
- Ente CLI v0.4.5, authenticated for Photos
- The restored legacy `state.sqlite3` from `~/.local/state/ente-camera-sync/`
- The existing `~/Pictures/Camera` tree

Run from the repository checkout:

```zsh
python3 ente-camera-audit.py \
  --legacy-db /path/to/restored/state.sqlite3 \
  --camera-root "$HOME/Pictures/Camera"
```

This calls the **verified v0.4.5** command
`ente photos file list --all --json --no-input`. The CLI must have access to
its existing account credentials. For multiple accounts, pass `--account NAME`.

If the metadata was already saved, the audit can be run without calling Ente:

```zsh
python3 ente-camera-audit.py \
  --legacy-db /path/to/restored/state.sqlite3 \
  --metadata-json /path/to/ente-file-list.json
```

To keep a detailed JSON audit report, which includes personal filenames and
paths, restrict permissions and redirect output explicitly:

```zsh
umask 077
python3 ente-camera-audit.py \
  --legacy-db /path/to/restored/state.sqlite3 \
  --json > ente-camera-audit.json
```

`--json` includes all Ente IDs and per-file results; ordinary console output
prints counts and up to 20 unmatched entries. Progress is printed to stderr.

## Matching rules

1. Open the **legacy SQLite** with SQLite `mode=ro`; read the `camera` rows
   only. The original database is not migrated or changed.
2. Read each regular file under Camera, **never follow symlinks**. Stream
   SHA-256 and BLAKE2b-512 in a single pass. On the first migration this
   requires reading every Camera file, approximately 85 GB for the currently
   recorded collection.
3. Compare SHA-256, byte length and nanosecond mtime against the old SQLite.
   Report missing old files, new local files and changed contents separately.
4. Match Ente `hash` (64-byte BLAKE2b, standard Base64) against **actual**
   local file contents. File IDs, names and dates are not used as proof of
   identity.
5. Live Photo `image:video` hashes are matched as two separate components.
6. Multiple distinct Ente IDs with the same type and hash are counted as
   duplicate content; albums are already deduplicated by the CLI list.

## Statuses

- `matched`: all required content is already present locally.
- `missing`: valid Ente hash, but no local content matches (candidate only).
- `partial_livephoto`: only one component was found locally.
- `no_hash`: Ente did not expose a content hash.
- `unsupported_hash`: Ente hash is not in the expected format.
- `unsupported_type`: type was not image/video/livephoto.

Local scan issues appear in `local_*` summary fields and in `scan_errors`.
If any file couldn't be read, the audit exits nonzero and missing results are
**not authoritative**. The results must be reviewed before implementing
actual downloads.

The next stage will introduce a separate writable SQLite cache and a
non-clobbering download protocol. This audit intentionally does neither.

## Tests

```zsh
python3 -m unittest discover -s tests -v
```

The tests use synthetic files and a temporary SQLite database. They do not
need Ente credentials or network access.
