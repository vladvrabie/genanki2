"""Functional wrappers over the models that make deck creation easier.

    pkg, collection = package()

`package()` returns a deck package and its collection. The package holds the media and
is read and written, and the collection holds the decks, notetypes and notes. The other
functions each build one model, taking its common options as parameters and passing any
other keyword argument to the model's protobuf config.
"""

from collections.abc import Iterable
from typing import Any

from genanki2.builtins import DEFAULT_DECK_CONFIG
from genanki2.errors import Genanki2Error
from genanki2.models.collection import Collection
from genanki2.models.deck import Deck, DeckFilteredKind, DeckNormalKind
from genanki2.models.deck_config import DeckConfig, DeckConfigInner
from genanki2.models.field import Field, FieldConfig
from genanki2.models.note import Note
from genanki2.models.notetype import Notetype, NotetypeConfig
from genanki2.models.package import Package
from genanki2.models.template import Template, TemplateConfig

__all__ = [
  "deck",
  "deck_config",
  "field",
  "filtered_deck",
  "note",
  "notetype",
  "package",
  "template",
]


def field(name: str, **kwargs: Any) -> Field:
  """Build a notetype field called `name`.

  Keyword arguments set `FieldConfig` options, such as `sticky` or `font_size`.
  """
  return Field(name=name, config=FieldConfig(**kwargs))


def template(name: str, front: str, back: str, **kwargs: Any) -> Template:
  """Build a card template called `name` from the formats of its two sides.

  `front` and `back` use Anki's template syntax: `{{Italian}}` inserts the field called
  Italian, and `{{FrontSide}}` on the back repeats the front.

  Keyword arguments set other `TemplateConfig` options, such as `q_format_browser`.
  """
  return Template(
    name=name, config=TemplateConfig(q_format=front, a_format=back, **kwargs)
  )


def notetype(
  name: str,
  fields: Iterable[Field | str],
  templates: Iterable[Template | tuple[str, str, str]],
  *,
  id: int | None = None,
  cloze: bool = False,
  sort_field: Field | str | None = None,
  **kwargs: Any,
) -> Notetype:
  """Build a notetype from its fields and card templates.

      vocabulary = notetype(
        "Vocabulary",
        ["Italian", "English"],
        [("Recognition", "{{Italian}}", "{{FrontSide}}<hr id=answer>{{English}}")],
        id=1712345678901,
      )

  A field is a name or a `Field`, and a template is a `Template` or a `(name, front,
  back)` tuple for `template()`. Objects are used as given, without a copy, so a `Field`
  or `Template` passed to two notetypes is shared by both, and changing it in one
  changes it in the other.

  With `cloze=True`, cards are numbered by cloze deletion and the styling is Anki's
  cloze styling (see `NotetypeConfig.new_cloze_config()`). The template has to use the
  `{{cloze:Field}}` filter.

  Important: Anki matches an incoming notetype by id. A notetype with a new id arrives
  as a separate notetype, and the notes already imported under the old one are skipped
  instead of updated. A rebuilt deck needs the same `id` every time: read it from the
  package written last time.

  Args:
    name: The name of the notetype.
    fields: The fields, in the order the editor shows them.
    templates: The card templates, one card type each.
    id: The notetype's id. Defaults to a new one.
    cloze: Make it a cloze notetype.
    sort_field: Which field the browser's Sort Field column shows. Defaults to the
      first.
    **kwargs: Other `NotetypeConfig` options, such as `css`.

  Raises:
    Genanki2Error: `fields` or `templates` is a single item rather than a list of them,
      a template is neither a `Template` nor a tuple of three strings, `kwargs` holds
      `kind` or holds `sort_field_idx` alongside `sort_field`, or `sort_field` names no
      field.
  """
  if isinstance(fields, (str, Field)):
    raise Genanki2Error(
      "`fields` takes a list of field names or `Field` objects. Wrap a single field "
      "in a list."
    )
  templates = list(templates)
  if not all(
    isinstance(t, Template)
    or (isinstance(t, tuple) and len(t) == 3 and all(isinstance(s, str) for s in t))  # pyright: ignore[reportUnnecessaryIsInstance]
    for t in templates
  ):
    raise Genanki2Error(
      "`templates` takes a list of `Template` objects or (name, front, back) tuples of "
      "strings. Wrap a single template in a list."
    )
  if sort_field is not None and "sort_field_idx" in kwargs:
    raise Genanki2Error(
      "`sort_field` and `sort_field_idx` both set the sort field. Pass one of them."
    )
  if "kind" in kwargs:
    raise Genanki2Error(
      "The notetype's kind is set by `cloze`. Pass `cloze=True` for a cloze notetype."
    )

  config: NotetypeConfig = (
    NotetypeConfig.new_cloze_config(**kwargs) if cloze else NotetypeConfig(**kwargs)
  )
  notetype_kwargs: dict[str, Any] = {} if id is None else {"id": id}
  notetype: Notetype = Notetype(
    name=name,
    fields=[f if isinstance(f, Field) else Field(name=f) for f in fields],
    templates=[t if isinstance(t, Template) else template(*t) for t in templates],
    config=config,
    **notetype_kwargs,
  )
  if sort_field is not None:
    notetype.set_sort_field(sort_field)
  return notetype


def note(
  notetype: Notetype,
  fields: Iterable[str],
  *,
  guid: str | None = None,
  tags: Iterable[str] | None = None,
) -> Note:
  """Build a note of `notetype` from its field values.

  `fields` holds one value per field, in the notetype's field order.
  `Notetype.field_values()` builds that list from a mapping of field names to values.

  Important: Anki matches an incoming note by `guid`, which defaults to a random one. A
  rebuilt deck that should update its notes in place passes the same guid every time:
  the one read from the package written last time, or one `guid_for()` derives from a
  key that names the note across rebuilds.

  Raises:
    Genanki2Error: `fields` or `tags` is a single string rather than a list of them.
  """
  if isinstance(fields, str) or isinstance(tags, str):
    name: str = "fields" if isinstance(fields, str) else "tags"
    raise Genanki2Error(
      f"`{name}` takes a list of strings. Wrap a single string in a list."
    )
  note_kwargs: dict[str, Any] = {}
  if guid is not None:
    note_kwargs["guid"] = guid
  if tags is not None:
    note_kwargs["tags"] = list(tags)
  return Note(notetype=notetype, fields=list(fields), **note_kwargs)


def deck_config(name: str, *, id: int | None = None, **kwargs: Any) -> DeckConfig:
  """Build a scheduling preset called `name`.

  Keyword arguments set `DeckConfigInner` options, such as `new_per_day` or
  `learn_steps`. The others keep the values Anki gives a new preset.

  Important: Anki matches an incoming preset by id, and on a match it keeps its own.
  A rebuilt deck passes the same id every time, the one read from the package written
  last time, so the import does not add another copy of the preset.
  """
  deck_config_kwargs: dict[str, Any] = {} if id is None else {"id": id}
  return DeckConfig(name=name, config=DeckConfigInner(**kwargs), **deck_config_kwargs)


def deck(
  name: str,
  config: DeckConfig | None = DEFAULT_DECK_CONFIG,
  *,
  description: str = "",
  **kwargs: Any,
) -> Deck:
  """Build a normal deck called `name`, scheduled by the `config` preset.

  `::` separates a parent deck from a subdeck, as in `Italian::Verbs`. Anki matches an
  incoming deck by name, so a rebuilt deck needs no stable id.

  `config` defaults to the `Default` preset. With `None`, the deck names its preset by
  `config_id` instead, and Anki schedules it by `Default` when the collection holds no
  preset with that id. `description` is the text shown under the deck's name on the
  study screen. Keyword arguments set other `DeckNormalKind` options, such as
  `new_limit`.

  Raises:
    Genanki2Error: `config` and `config_id` are both given.
  """
  if config is not None and "config_id" in kwargs:
    raise Genanki2Error(
      "`config` and `config_id` both set the deck's preset. Pass `config=None` to use "
      "`config_id`."
    )
  kind: DeckNormalKind = DeckNormalKind(description=description, **kwargs)
  return Deck(name=name, kind=kind, config=config)


def filtered_deck(
  name: str,
  search: str,
  limit: int = 100,
  order: DeckFilteredKind.SearchTerm.Order = DeckFilteredKind.SearchTerm.Order.RANDOM,
  search2: str | None = None,
  limit2: int = 20,
  order2: DeckFilteredKind.SearchTerm.Order = DeckFilteredKind.SearchTerm.Order.DUE,
  **kwargs: Any,
) -> Deck:
  """Build a filtered deck called `name`, which gathers the cards `search` matches.

      filtered_deck("Italian::Cram verbs", "deck:Italian tag:verb", limit=50)

  The deck stays empty until the user builds it in Anki. `search` uses the browser's
  syntax, and an empty one matches every card. Anki never gathers suspended or buried
  cards, or cards already in another filtered deck. Up to `limit` cards are gathered,
  picked by `order`. `search2` adds a second search, gathering up to `limit2` more
  cards by `order2`. Without `search2`, `limit2` and `order2` are ignored. Keyword
  arguments set other `DeckFilteredKind` options, such as `reschedule`.

  An apkg import ignores empty decks, so a filtered deck written this way reaches the
  user:
  - if the package holds a card in it belonging to the filtered deck (`Card.deck` is
    set to the filtered deck and `Card.original_*` attributes are set to its home deck).
    Moreover, it stays a filtered deck during import only when the user ticks "Import
    any learning progress", otherwise the import turns it into a normal deck.
  - through a collection package.
  """
  SearchTerm = DeckFilteredKind.SearchTerm
  search_terms: list[DeckFilteredKind.SearchTerm] = [
    SearchTerm(search=search, limit=limit, order=order)
  ]
  if search2 is not None:
    search_terms.append(SearchTerm(search=search2, limit=limit2, order=order2))
  return Deck(name=name, kind=DeckFilteredKind(search_terms=search_terms, **kwargs))


def package() -> tuple[Package, Collection]:
  """Start a deck package, returning it with its collection.

      pkg, collection = package()

  The collection starts from `Collection.default()`. For a collection package, build
  `CollectionPackage(collection=Collection.anki_default())` instead.
  """
  package: Package = Package(collection=Collection.default())
  return package, package.collection
