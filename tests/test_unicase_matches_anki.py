"""Does a Python collation order text the same way Anki's Rust `unicase` does?

    uv run --with anki --with pytest pytest tests/test_unicase_matches_anki.py -s

SQLite stores only the collation *name* in the file, so genanki2 has to register its
own `unicase` to execute the DDL (stdlib sqlite3 rejects an unknown collation name).
That comparator is not decorative: SQLite uses it to physically order index entries,
so where it disagrees with Anki's, Anki's lookups through that index can miss rows.

Anki pins the crate exactly for the same reason, in Cargo.toml:

    unicase = "=2.6.0" # any changes could invalidate sqlite indexes

Anki is the oracle here: a collection opened by the real library has the real
collation registered (rslib/src/storage/sqlite.rs:87), so SQL run through it answers
what Rust would say. anki is a test-only dependency, never a dependency of genanki2.
"""

import tempfile
from pathlib import Path

import pytest
from anki.collection import Collection

# Strings chosen to stress where case folding implementations usually diverge.
CORPUS = [
  "ciao",
  "Ciao",
  "CIAO",
  "arger",
  "ärger",
  "Ärger",
  "ARGER",
  "strasse",
  "straße",
  "STRASSE",
  "perche",
  "perché",
  "Perché",
  "i",
  "I",
  "ı",  # Turkish dotless i
  "İ",  # Turkish dotted capital I
  "sigma",
  "σ",
  "ς",  # Greek final sigma
  "Σ",
  "ä",  # precomposed
  "ä",  # a + combining diaeresis, same glyph
  "привет",
  "Привет",
  "日本語",
  "",
  " ",
  "deck::sub",
  "Deck::Sub",
]

CANDIDATES = {
  "casefold": lambda s: s.casefold(),
  "lower": lambda s: s.lower(),
}


def sign(n: int) -> int:
  return (n > 0) - (n < 0)


@pytest.fixture(scope="module")
def anki_cmp():
  """Pairwise comparison as Anki's registered `unicase` collation sees it."""
  with tempfile.TemporaryDirectory() as tmp:
    col = Collection(str(Path(tmp) / "collection.anki2"))

    def cmp(a: str, b: str) -> int:
      return col.db.scalar(
        "select case when ? = ? collate unicase then 0"
        " when ? < ? collate unicase then -1 else 1 end",
        a,
        b,
        a,
        b,
      )

    yield cmp
    col.close()


def divergences(anki_cmp, key):
  out = []
  for i, a in enumerate(CORPUS):
    for b in CORPUS[i + 1 :]:
      ours = sign((key(a) > key(b)) - (key(a) < key(b)))
      theirs = sign(anki_cmp(a, b))
      if ours != theirs:
        out.append((a, b, ours, theirs))
  return out


@pytest.mark.parametrize("name", sorted(CANDIDATES))
def test_report_divergences(anki_cmp, name):
  found = divergences(anki_cmp, CANDIDATES[name])
  print(
    f"\n{name}: {len(found)} divergent pair(s) out of {len(CORPUS) * (len(CORPUS) - 1) // 2}"
  )
  for a, b, ours, theirs in found:
    print(f"  {a!r} vs {b!r}: ours={ours} anki={theirs}")


def test_best_candidate_agrees_with_anki(anki_cmp):
  """The comparator genanki2 ships must match Anki on the whole corpus."""
  best = min(CANDIDATES, key=lambda n: len(divergences(anki_cmp, CANDIDATES[n])))
  assert divergences(anki_cmp, CANDIDATES[best]) == [], f"best candidate was {best}"
