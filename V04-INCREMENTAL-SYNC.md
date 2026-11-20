# Ente Photos v0.4 — add-only incremental download (preview)

This is an **opt-in**, separate implementation from the legacy `ente export`
synchronizer. It needs Ente CLI **v0.4.5**. Nothing in this PR installs or
enables a timer, replaces `ente-camera-sync.py`, or changes the old database.

## Safety model

- `init` hashes all existing Camera files and compares SHA-256, size, and
  nanosecond mtime against the **read-only** legacy SQLite. It refuses any
  discrepancy and only then atomically creates a **different** database:
  `~/.local/state/ente-camera-sync/state-v04.sqlite3`. If that DB already
  exists, initialization refuses to overwrite it. This necessarily rereads
  the approximately 85 GB Camera tree once.
- `sync` is **dry-run by default**. Without `--apply`, it opens the new DB in
  SQLite read-only mode and never changes Camera. The Ente CLI may update its
  own encrypted metadata replica during online listing.
- `sync --apply` uses the metadata hashes to identify already-local originals.
  Only missing Ente file IDs are downloaded to private temporary directories
  *inside* the destination filesystem, verified using BLAKE2b-512, and
  atomically hard-linked into place (no overwrite). The SQLite index records
  each published file **after** linking it, allowing safe retries after a
  crash. Temporary directories are removed after normal completion; an
  abrupt power loss can leave `.ente-camera-v04-*` directories to inspect
  and remove manually.
- Ente deletion never deletes any Camera file. No upload or remote mutation
  is implemented. Multiple Ente file IDs containing identical content share
  local content rather than being downloaded repeatedly.
- All Ente records include an explicit fixed account selector in the DB.
  Changing `--account` for a given database is refused. Use an account name
  visible via `ente account list`.
- Images/videos are saved under `YYYY/MM` based on Ente's `createdAt` in
  `Asia/Tokyo`; invalid dates go to `unknown-date`. Existing files are never
  moved or renamed. Live Photos from Ente CLI are ZIP downloads; the script
  identifies image and video components by hashes and verifies both **before**
  publishing. This path has synthetic tests; actual Live Photos have **not**
  been tested against a real Ente account.
- The SQLite cache relies on file size and nanosecond mtime to avoid repeated
  hashing. It rehashes files whose metadata changed or whose paths became
  unavailable. It cannot detect a change whose size and mtime are both
  preserved; use a future full-verify operation for periodic scrubs. It does
  not automatically discover newly added local-only files; that will be a
  separate explicit scan feature for the planned selective upload workflow.

## 1. Obtain your Ente CLI account name

```zsh
ente account list
```

Use the same value in **every** command below, replacing `ACCOUNT_NAME`.

## 2. Initialize the new SQLite database

Only after you have confirmed the read-only audit is clean:

```zsh
python3 ente-camera-sync-v04.py \
  --account 'ACCOUNT_NAME' \
  init \
  --legacy-db "$HOME/develop/backup/ente-camera-sync-test/state.sqlite3"
```

This step **only** creates the new SQLite; it never writes to Camera or the
legacy DB. If you want an isolated trial database, use `--db /path/to/test.sqlite3`
**before** `init`, and pass the same `--db` to later sync commands.

## 3. Preview the download plan

```zsh
python3 ente-camera-sync-v04.py --account 'ACCOUNT_NAME' sync --dry-run
```

Expected initial counts, if nothing in the collection changed since your
previous audit: roughly 7,873 matched and 22 missing. If new images were
uploaded to Ente since then, the missing count may be larger.

## 4. Download ONE Ente item

```zsh
python3 ente-camera-sync-v04.py \
  --account 'ACCOUNT_NAME' \
  sync --apply --max-downloads 1
```

Run the dry-run again. You should now see one fewer missing file. After
inspecting the saved result, the remaining items can be fetched with:

```zsh
python3 ente-camera-sync-v04.py --account 'ACCOUNT_NAME' sync --apply
```

A repeated `sync --apply` should report zero new downloads if nothing has
changed on Ente. No download runs until you explicitly supply `--apply`.

## Behavior on errors

- Hash mismatch: do not publish the downloaded file; report an error.
- Name collision with different bytes: preserve the existing file and append
  `__ente_<file-id>` to the newly downloaded filename.
- Partial Live Photo or interruption: retain already published components,
  verify them against the DB on the next run, and retry the missing component.
- Invalid or duplicated Ente file IDs, wrong account, a changed Camera root,
  or a suspiciously empty Ente listing: refuse processing.
- Errors result in a nonzero exit status, without deleting local files.

**Current limitation:** the `sync` database index only covers the files
indexed by `init` and subsequent verified downloads; newly added local files
with different names are not discovered until a future explicit local scan.
For normal backup usage that means existing Camera files are detected but a
manual import not yet indexed may be downloaded again if it exists in Ente.

## Tests

```zsh
python3 -m unittest discover -s tests -v
```

The offline tests simulate the Ente download CLI. They do not require Ente
credentials, touch any user's Camera files, or make real network requests.

## Scheduling

No systemd unit is changed or enabled by this PR. Review the results in manual
mode first. When migrating scheduled jobs, disable the old exporter timer
before configuring a new v0.4 timer, to avoid running both implementations.