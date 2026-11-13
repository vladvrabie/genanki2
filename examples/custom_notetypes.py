"""Create custom notetypes: a normal one with styled fields and a cloze one.

    python examples/custom_notetypes.py notetypes.apkg

A notetype contains fields and templates:

- A field is just a name (string) or a `field()` call when it needs options.
- A template is a tuple of a name, a front and a back, or alternatively a `template()`
  call when it needs options.

The keyword arguments of `field()`, `template()` and `notetype()` are the options of
`FieldConfig`, `TemplateConfig` and `NotetypeConfig`.

Use `builtin_notetype()` to create a copy of one of Anki's own notetypes that you can
change and customize.

For notetypes that need updates, make sure to set the same id every time. See
examples/language_deck.py.

A normal notetype makes one card per template whose front is not empty for the note.
A cloze notetype makes one card per cloze number in the note's fields, so the note
below gets two.
"""

import sys
from pathlib import Path

from genanki2 import deck, field, note, notetype, package

# Stable notetype ids for reproducible packages that update in Anki, not duplicate.
# The same (but for different attributes) is needed for decks and notes. See
# examples/language_deck.py.
IT_PHRASE_NOTETYPE_ID = 1_759_600_000_001
IT_CLOZE_NOTETYPE_ID = 1_759_600_000_002


def main(path: Path) -> None:
  pkg, collection = package()
  italian = deck("Italian")

  phrase = notetype(
    "Italian phrase",
    [
      "Italian",
      "English",
      field("Notes", collapsed=True, plain_text=True),
    ],
    [("Translate", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}")],
    id=IT_PHRASE_NOTETYPE_ID,
    css=".card { font-size: 24px; }",
    # The browser shows and sorts by English in its Sort Field column.
    sort_field="English",
  )

  cloze = notetype(
    "Italian cloze",
    ["Text", "Translation"],
    [("Cloze", "{{cloze:Text}}", "{{cloze:Text}}<br>{{Translation}}")],
    id=IT_CLOZE_NOTETYPE_ID,
    cloze=True,
  )

  note1 = note(phrase, ["Buongiorno!", "Good morning!", ""])
  fields = [
    "{{c1::Roma}} è la capitale d'{{c2::Italia}}.",
    "Rome is the capital of Italy.",
  ]
  note2 = note(cloze, fields)
  collection.add_notes([note1, note2], italian)

  pkg.write_apkg(path)


if __name__ == "__main__":
  main(Path(sys.argv[1]))
