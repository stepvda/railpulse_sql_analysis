# `data/railpulse.db`, in pieces

The built database is 980 MB — too big for git, and `.gitignore` keeps it out.
This directory holds the same file as a 95 MB zip cut into 25 MB parts, which
git is happy to carry.

```bash
make db-restore          # parts -> data/railpulse.db, checksum-verified
make db-verify-parts     # checksum the parts without unpacking (fast)
```

`make build` is still the primary way to get a database: it downloads the GTFS
feed and reproduces this file in about three minutes. These parts exist for the
cases where that is not practical — a reviewer on a slow connection, a machine
without an API key, or wanting exactly the snapshot the report was written
against rather than today's feed.

## What is here

| File                        | What it is                                    |
| --------------------------- | --------------------------------------------- |
| `railpulse.db.zip.000`–`003` | 25 MB byte-ranges of one ordinary zip archive |
| `SHA256SUMS`                | checksums of the parts, for `shasum -c`       |
| `railpulse.db.sha256`       | checksum of the *restored* database           |

Restoring is verified twice: the parts are checked before reassembly, and the
extracted database is checked against `railpulse.db.sha256` afterwards, so a
truncated download fails loudly instead of producing a subtly broken SQLite file.

## Restoring by hand

The parts are byte-ranges of a single stream, so nothing clever is needed:

```bash
cat data/db_parts/railpulse.db.zip.* > railpulse.db.zip
unzip railpulse.db.zip -d data/
```

On Windows: `copy /b railpulse.db.zip.* railpulse.db.zip`, then any unzip tool.

The zero-padded numeric suffixes are what make the shell glob safe — they sort
lexicographically into numeric order, so the pieces concatenate correctly even
past `.009`.

## Why not `zip -s 25m`

A native multi-part zip (`.z01`, `.z02`, …) is the obvious format and is what
this started as. It was abandoned because the `zip` 3.0 that Apple ships cannot
read its own output back: merging four parts with the documented
`zip -s 0 archive.zip --out merged.zip` silently dropped two of them — 99.6 MB
in, 52.4 MB out — and the result failed to inflate. Concatenating the raw parts
almost works, but `unzip` has to "re-compensate" for the per-disk header offsets
and exits non-zero doing it.

One ordinary zip split into fixed-size blocks has neither problem: `cat`
reassembles it exactly, any unzip on any platform reads the result, and there is
no dependence on a particular zip build. The trade-off is that no tool can open
a single part on its own — you have to reassemble first, which `make db-restore`
does for you.

## Regenerating

After a rebuild that changes the database:

```bash
make db-split            # re-cuts the parts and rewrites both checksum files
```

The output is deterministic — the same database always produces byte-identical
parts — so a no-op rebuild leaves nothing for git to commit.
