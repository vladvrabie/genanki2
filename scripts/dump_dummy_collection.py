#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["anki"]
# ///
"""The schema 11 collection a package carries for clients too old to read it.

    uv run scripts/dump_dummy_collection.py

A v18 package declares its version in the `meta` zip entry and keeps the collection
in `collection.anki21b`. A client too old to know about `meta` looks for
`collection.anki2` and shows a single note and card that asks the user to update.
The modern importer never opens it.

Builds that collection with the official `anki` package, replaces every timestamp value
with a constant and writes it to src/genanki2/_dummy_collection.anki2.
genanki2 copies the file into the package as it is.

Output is deterministic for a given Anki version. Re-running it after an Anki upgrade
can produce a diff: no diff means the stub is unchanged, a diff is the new stub.
"""

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from anki.collection import Collection
from anki.decks import DeckId
from anki.models import NotetypeDict
from anki.notes import Note

# Fix ids and modification times, so that the dump is the same file every time.
FROZEN_MILLIS: int = 1_000_000_000_000
FROZEN_SECONDS: int = 1_000_000_000
FROZEN_NOTETYPE_ID: int = FROZEN_MILLIS + 1
FROZEN_GUID: str = "genanki2dummy"


def dump_dummy_collection(path: Path) -> Path:
  """Build the collection at `path` with one Basic note in the default deck, schema 11."""
  col: Collection = Collection(str(path))
  basic_nt: NotetypeDict | None = col.models.by_name("Basic")
  assert basic_nt is not None, "A fresh collection has no Basic notetype"
  note: Note = col.new_note(basic_nt)
  note["Front"] = col.tr.exporting_colpkg_too_new()
  col.add_note(note, DeckId(1))
  col.close(downgrade=True)
  return path


def canonical_json(value: Any) -> str:
  """Serialize `value` with its keys sorted and no padding.

  Anki's iteration order of the columns is not stable between runs.
  """
  return json.dumps(value, sort_keys=True, separators=(",", ":"))


def frozen_notetypes(models: str) -> dict[str, Any]:
  """Remove all notetypes except the Basic one and freeze fields, templates and its ids."""
  basic: dict[str, Any] = next(
    notetype for notetype in json.loads(models).values() if notetype["name"] == "Basic"
  )
  basic["id"] = FROZEN_NOTETYPE_ID
  basic["mod"] = FROZEN_SECONDS
  basic["usn"] = -1
  for ordinal, field in enumerate(basic["flds"], start=1):
    field["id"] = ordinal
  for ordinal, template in enumerate(basic["tmpls"], start=1):
    template["id"] = ordinal
  return {str(FROZEN_NOTETYPE_ID): basic}


def frozen_conf(conf: str) -> dict[str, Any]:
  """Point `conf`'s current notetype at the frozen id and drop the per-run keys.

  Anki adds some per-run keys (`_deck_1_lastNotetype`, `_nt_<id>_lastDeck`) when this
  run adds the note to the collection.
  """
  entries: dict[str, Any] = json.loads(conf)
  frozen: dict[str, Any] = {
    key: value
    for key, value in entries.items()
    if not key.startswith(("_deck_", "_nt_"))
  }
  frozen["curModel"] = FROZEN_NOTETYPE_ID
  return frozen


def freeze_col_row(db: sqlite3.Connection) -> None:
  """Pin the collection row's timestamps and rewrite its four JSON columns.

  All JSON columns are re-serialized to guarantee stable key order.
  """
  query: str = "SELECT models, conf, decks, dconf FROM col"
  models, conf, decks, dconf = db.execute(query).fetchone()
  db.execute(
    "UPDATE col SET crt = ?, mod = ?, scm = ?, ls = 0, usn = -1,"
    " models = ?, conf = ?, decks = ?, dconf = ?",
    (
      FROZEN_SECONDS,
      FROZEN_MILLIS,
      FROZEN_MILLIS,
      canonical_json(frozen_notetypes(models)),
      canonical_json(frozen_conf(conf)),
      canonical_json(json.loads(decks)),
      canonical_json(json.loads(dconf)),
    ),
  )


def freeze_note_and_card(db: sqlite3.Connection) -> None:
  """Pin the ids and timestamps of the one note and the card it generates.

  Freezes the guid too, so an old client updates the note instead of adding a new one.
  """
  db.execute(
    "UPDATE notes SET id = ?, guid = ?, mid = ?, mod = ?, usn = -1",
    (FROZEN_MILLIS, FROZEN_GUID, FROZEN_NOTETYPE_ID, FROZEN_SECONDS),
  )
  db.execute(
    "UPDATE cards SET id = ?, nid = ?, mod = ?, usn = -1",
    (FROZEN_MILLIS, FROZEN_MILLIS, FROZEN_SECONDS),
  )


def freeze_db(db: sqlite3.Connection) -> sqlite3.Connection:
  """Replace ids and modification times that are timestamps with constants.

  It makes the dump reproducible. A diff in the generated file reports a change in Anki.
  """
  freeze_col_row(db)
  freeze_note_and_card(db)
  db.commit()
  return db


def compact_db(db: sqlite3.Connection) -> sqlite3.Connection:
  """Repack `db` into 512-byte pages like Anki does."""
  db.executescript("pragma page_size=512; pragma journal_mode=delete; vacuum")
  return db


def main() -> int:
  """Build the stub, freeze it, compact it, and write `_dummy_collection.anki2`."""
  with tempfile.TemporaryDirectory() as tmp:
    path: Path = Path(tmp) / "collection.anki2"
    dump_dummy_collection(path)
    db: sqlite3.Connection = sqlite3.connect(path)
    freeze_db(db)
    compact_db(db)
    db.close()
    bincontent: bytes = path.read_bytes()

  project_path: Path = Path(__file__).resolve().parent.parent
  binfile: Path = project_path / "src" / "genanki2" / "_dummy_collection.anki2"
  binfile.parent.mkdir(parents=True, exist_ok=True)
  changed: bool = not binfile.exists() or binfile.read_bytes() != bincontent
  binfile.write_bytes(bincontent)
  print(f"{'updated' if changed else 'unchanged'}: {binfile}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
