#!/usr/bin/env bash
# ===========================================================================
# RailPulse — ship the 1 GB database through GitHub in 25 MB pieces
# ===========================================================================
# The database is a 980 MB SQLite file. It is git-ignored (see .gitignore) and
# `make build` reproduces it from the GTFS feed in about three minutes, which
# is the intended way to get one. This script covers the case where that is not
# practical: reviewers on a slow connection, a machine without the API key, or
# an archived snapshot of exactly the data the report was written against.
#
#   ./scripts/db_parts.sh split      data/railpulse.db  ->  data/db_parts/*
#   ./scripts/db_parts.sh restore    data/db_parts/*    ->  data/railpulse.db
#   ./scripts/db_parts.sh verify     check the parts against SHA256SUMS
#
# WHY A PLAIN ZIP + `split`, AND NOT `zip -s 25m`
# `zip -s` makes a native multi-part archive (.z01, .z02, …, .zip) which is the
# obvious choice, and it is what this started as. It was abandoned because the
# `zip` 3.0 that Apple ships cannot reliably read its own output back: merging
# the four parts with the documented `zip -s 0 archive.zip --out merged.zip`
# silently dropped two of them (99.6 MB in, 52.4 MB out) and the result failed
# to inflate. Concatenating the raw parts *almost* works, but unzip has to
# "re-compensate" for the per-disk header offsets and exits non-zero doing it.
#
# One ordinary zip cut into fixed-size blocks has neither problem. The pieces
# are byte-ranges of a single stream, so `cat` reassembles them exactly, any
# unzip on any platform reads the result, and there is no dependence on a
# particular zip build. The cost is that no tool opens a part on its own — you
# must reassemble first, which `restore` does for you.
#
# WHY 25 MB
# Comfortably under GitHub's 50 MB warning and 100 MB hard limit per file, with
# room for the repository to be browsed over a bad connection.
# ===========================================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

DB=data/railpulse.db
PARTS_DIR=data/db_parts
STEM=railpulse.db.zip          # parts are $STEM.000, $STEM.001, …
PART_SIZE=25m
DB_HASH_FILE="$PARTS_DIR/railpulse.db.sha256"

# macOS ships `shasum`, most Linux images ship `sha256sum`, and CI images vary.
if command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$@"; }
  sha256_check() { shasum -a 256 -c "$@"; }
elif command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$@"; }
  sha256_check() { sha256sum -c "$@"; }
else
  echo "error: neither shasum nor sha256sum found" >&2
  exit 1
fi

hash_of() { sha256 "$1" | awk '{print $1}'; }

# ---------------------------------------------------------------------------
do_split() {
  [[ -f $DB ]] || { echo "error: $DB not found — run 'make build' first" >&2; exit 1; }

  # A live -wal file means committed transactions are sitting outside the .db,
  # so the bytes we are about to archive are not the database anyone would read.
  if [[ -s $DB-wal ]]; then
    echo "error: $DB-wal is non-empty; checkpoint first:" >&2
    echo "       sqlite3 $DB 'pragma wal_checkpoint(truncate);'" >&2
    exit 1
  fi

  # Deliberately NOT `local`: the EXIT trap runs after this function has
  # returned, where a local $tmp is out of scope — under `set -u` that turns a
  # successful run into "tmp: unbound variable" and leaks the temp directory.
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT

  # `split -d` (numeric suffixes) is GNU coreutils and macOS 13+. The
  # alphabetic fallback would silently change every part's filename, so fail
  # loudly rather than commit a set that `restore` on another box won't glob.
  if ! split -d -b 1 /dev/null "$tmp/probe" 2>/dev/null; then
    echo "error: this 'split' lacks -d (numeric suffixes); install coreutils" >&2
    exit 1
  fi

  echo "==> compressing $DB (~90 s, deflate -9)"
  # -j stores the bare name "railpulse.db" with no leading data/, so `restore`
  # controls the destination via `unzip -d` instead of trusting the archive.
  zip -9 -j "$tmp/$STEM" "$DB"

  echo "==> splitting into $PART_SIZE pieces"
  mkdir -p "$PARTS_DIR"
  rm -f "$PARTS_DIR/$STEM".[0-9][0-9][0-9]
  split -b "$PART_SIZE" -d -a 3 "$tmp/$STEM" "$PARTS_DIR/$STEM."

  echo "==> writing checksums"
  ( cd "$PARTS_DIR" && sha256 "$STEM".[0-9][0-9][0-9] > SHA256SUMS )
  # The hash of the *restored* file, so `restore` can prove the round trip
  # rather than merely proving the parts arrived intact. Written in shasum's
  # own "<hash>  <name>" form so `cd data && shasum -c db_parts/…` also works.
  sha256 "$DB" | awk '{print $1 "  railpulse.db"}' > "$DB_HASH_FILE"

  ls -lh "$PARTS_DIR"
}

# ---------------------------------------------------------------------------
do_verify() {
  [[ -f $PARTS_DIR/SHA256SUMS ]] || { echo "error: $PARTS_DIR/SHA256SUMS not found" >&2; exit 1; }
  ( cd "$PARTS_DIR" && sha256_check SHA256SUMS )
}

# ---------------------------------------------------------------------------
do_restore() {
  shopt -s nullglob
  local parts=( "$PARTS_DIR/$STEM".[0-9][0-9][0-9] )
  shopt -u nullglob
  (( ${#parts[@]} )) || { echo "error: no parts in $PARTS_DIR" >&2; exit 1; }

  if [[ -f $DB ]]; then
    echo "error: $DB already exists — remove it first (make clean-db)" >&2
    exit 1
  fi

  echo "==> verifying ${#parts[@]} parts"
  do_verify

  tmp=$(mktemp -d)          # global, not local — see the note in do_split
  trap 'rm -rf "$tmp"' EXIT

  # The glob is what guarantees ordering: zero-padded numeric suffixes sort
  # lexicographically into numeric order, so .000 .001 .010 .100 stay correct.
  echo "==> reassembling"
  cat "${parts[@]}" > "$tmp/$STEM"

  echo "==> extracting to $(dirname "$DB")/"
  unzip -o -d "$(dirname "$DB")" "$tmp/$STEM"

  if [[ -f $DB_HASH_FILE ]]; then
    local want got
    want=$(awk '{print $1}' "$DB_HASH_FILE")
    got=$(hash_of "$DB")
    if [[ $want != "$got" ]]; then
      echo "error: restored database hash mismatch" >&2
      echo "       expected $want" >&2
      echo "       got      $got" >&2
      exit 1
    fi
    echo "==> sha256 matches the archived database"
  fi

  # Cheap structural check; `make verify` is the real one, but a database that
  # fails quick_check is a broken restore rather than a data problem.
  if command -v sqlite3 >/dev/null 2>&1; then
    echo "==> sqlite quick_check: $(sqlite3 "$DB" 'pragma quick_check;')"
  fi

  ls -lh "$DB"
  echo
  echo "Next:  make verify      # 21 integrity assertions"
}

# ---------------------------------------------------------------------------
case "${1:-}" in
  split)   do_split   ;;
  restore) do_restore ;;
  verify)  do_verify  ;;
  *)
    echo "usage: $(basename "$0") {split|restore|verify}" >&2
    exit 2
    ;;
esac
