#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["anki"]
# ///
"""The rows of an empty Anki collection, dumped as reference.

    uv run scripts/dump_empty_collection_rows.py

Creates a throwaway collection with the official `anki` package, reads its tables and
writes the rows to src/genanki2/_default_rows.py.

Output is deterministic for a given Anki version. Re-running it after an Anki
upgrade can produce a diff: no diff means the rows are unchanged, a diff is the
list of things genanki2 needs to update.
"""

import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, TypeAlias

import anki.buildinfo
from anki import notetypes_pb2
from anki.collection import Collection

Row: TypeAlias = dict[str, Any]
Rows: TypeAlias = list[Row]


def read_table_names(db: sqlite3.Connection) -> list[str]:
  """List every user table in `db`, skipping SQLite's own."""
  query: str = (
    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
  )
  return [row[0] for row in db.execute(query).fetchall()]


def read_rows(db: sqlite3.Connection, table: str) -> Rows:
  """Read every row of `table` as a dict, in a fixed order."""
  ordering_clause: dict[str, str] = {
    "fields": "ORDER BY ntid, ord",
    "templates": "ORDER BY ntid, ord",
    "config": "ORDER BY KEY",
    "tags": "ORDER BY tag",
    "graves": "ORDER BY oid, type",
  }
  query: str = f"SELECT * FROM {table} {ordering_clause.get(table, 'ORDER BY id')}"
  return [dict(row) for row in db.execute(query).fetchall()]


def read_tables(db: sqlite3.Connection) -> dict[str, Rows]:
  """Read every table in `db` into a `{table: rows}` mapping."""
  return {table: read_rows(db, table) for table in read_table_names(db)}


def fix_timestamps(tables: dict[str, Rows]) -> None:
  """Zero every timestamp so the dump is reproducible."""
  tables["col"][0]["crt"] = 0
  tables["col"][0]["scm"] = 0
  for row in tables["config"]:
    if row["KEY"] == "creationOffset":
      row["val"] = b"0"
    elif row["KEY"] == "curModel":
      row["mtime_secs"] = 0


def fix_notetype_ids(tables: dict[str, Rows]) -> None:
  """Renumber notetype ids sequentially from 1 to 6 and update every id reference.

  Anki matches notetypes by id. It generates notetype ids using timestamps, so
  renumbering them 1 to 6 avoids collisions with the user's notetypes.

  With stable unique ids, the script is deterministic and the user's collection is not
  clobbered with duplicate notetype names (Basic+, Basic++ etc.) every time a
  genanki2-generated package is imported.
  """
  old2new_id: dict[int, int] = {}
  for i, row in enumerate(tables["notetypes"], start=1):
    old2new_id[row["id"]] = i
    row["id"] = i
  for row in tables["fields"]:
    row["ntid"] = old2new_id[row["ntid"]]
  for row in tables["templates"]:
    row["ntid"] = old2new_id[row["ntid"]]
  for row in tables["config"]:
    if row["KEY"] == "curModel":
      new_id: int = old2new_id[int(row["val"].decode())]
      row["val"] = str(new_id).encode()


def fix_random_ids(tables: dict[str, Rows]) -> None:
  """Clear the random `id` Anki assigns to each field and template config."""
  # On import, Anki will fall back to matching by name
  for row in tables["fields"]:
    cfg = notetypes_pb2.Notetype.Field.Config.FromString(row["config"])
    cfg.ClearField("id")
    row["config"] = cfg.SerializeToString()

  for row in tables["templates"]:
    cfg = notetypes_pb2.Notetype.Template.Config.FromString(row["config"])
    cfg.ClearField("id")
    row["config"] = cfg.SerializeToString()


def add_notetype_namespace(tables: dict[str, Rows]) -> None:
  """Ensure note types have a distinct name from the Anki generated built-in ones.

  They appear clearly and consistently labeled to the user in Anki's UI.
  """
  for row in tables["notetypes"]:
    row["name"] += " (genanki2)"


def update_notetype_mtime_version(tables: dict[str, Rows]) -> None:
  """Stamp each notetype modification time with the version genanki2 ships it at.

  An import compares the incoming notetype's modification time against the one in the
  target collection and replaces it only when the incoming value is larger, storing it
  as it arrives. A number that stays put across dumps therefore means a package that
  never touches the six notetypes a user already has (edits are also not touched).
  Raising the version is the way a corrected builtin reaches that collection,
  at the cost of whatever the user changed in it. So the values below move by hand,
  when the content of a builtin changes, and never with the clock.

  Runs after `fix_notetype_ids()`, since the versions are keyed by the renumbered id,
  and the same six numbers are declared as `BUILTIN_NOTETYPES_VERSIONS` in
  `src/genanki2/builtins.py`. Update both together.
  """
  id2mtime_version: dict[int, int] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
  for row in tables["notetypes"]:
    row["mtime_secs"] = id2mtime_version[row["id"]]


def dump_empty_collection(db: sqlite3.Connection) -> str:
  """Render `db`'s tables, fixed up for genanki2, as the `_default_rows.py` source."""
  schema_version: int = db.execute("SELECT ver FROM col").fetchone()[0]
  lines: list[str] = [
    "# Generated by scripts/dump_empty_collection_rows.py, do not edit.",
    f"# anki {anki.buildinfo.version}, collection schema version {schema_version}\n",
  ]

  tables: dict[str, Rows] = read_tables(db)
  # Determinism fixes
  fix_timestamps(tables)
  fix_notetype_ids(tables)
  fix_random_ids(tables)
  # genanki2 customizations
  add_notetype_namespace(tables)
  update_notetype_mtime_version(tables)

  for table, rows in tables.items():
    lines.append(f"# ========= TABLE {table} =========")
    if not rows:
      lines.append(f"table_{table} = []\n")
    else:
      lines.append(f"table_{table} = [")
      lines += [f"  {row}," for row in rows]
      lines.append("]\n")
  return "\n".join(lines)


def main() -> int:
  """Create a throwaway collection and write its default rows to `_default_rows.py`."""
  with tempfile.TemporaryDirectory() as tmp:
    path: Path = Path(tmp) / "collection.anki2"
    col: Collection = Collection(str(path))
    col.close()
    db: sqlite3.Connection = sqlite3.connect(path)
    db.create_collation(
      "unicase",
      lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()),
    )
    db.row_factory = sqlite3.Row  # needed by read_rows()
    pycontent: str = dump_empty_collection(db)
    db.close()

  project_path: Path = Path(__file__).resolve().parent.parent
  pyfile: Path = project_path / "src" / "genanki2" / "_default_rows.py"
  pyfile.parent.mkdir(parents=True, exist_ok=True)
  changed: bool = not pyfile.exists() or pyfile.read_text() != pycontent
  pyfile.write_text(pycontent)
  print(f"{'updated' if changed else 'unchanged'}: {pyfile}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
