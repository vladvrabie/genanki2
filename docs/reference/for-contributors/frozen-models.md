# Frozen models: limitations and possible solutions

Every genanki2 model inherits `freeze()` from `DeepFreezable` (`src/genanki2/freezable.py`). A frozen model:

- refuses assigning or deleting a public attribute
- replaces its lists, dicts and sets with tuples, `frozendict`s and `frozenset`s
- freezes every model it holds

The builtin constants are frozen where `builtins.py` builds them. Behaviour below was measured on pydantic 2.13.4.

## Writes that go around the guard

The guard sits in `__setattr__` and `__delattr__`, so anything that writes the instance `__dict__` directly passes it. For example, `pydantic.BaseModel.model_copy(update=...)` does that on the copy it returns, or any `instance.__dict__[name] = value` assignment.

The freeze protects against accidental writes, but it is not a security boundary.

For the builtin notetypes, the one value a writer has reason to set, the modification time, is checked again at write time by `Package.check_builtin_notetype_versions()`.

`BaseModel.model_copy()` also copies the flag, so a copy of a frozen model is frozen. `builtin_notetype()`, `default_deck()` and `default_deck_config()` build a new, writable model instead of copying.

## A frozen model is not equal to its unfrozen twin

`[a] == (a,)` is `False` in Python, so once `deepfreeze()` has swapped a list for a tuple, a frozen model compares unequal to an unfrozen one, even if they hold the same values.

To compare content and not structure, `DeepFreezable` has `is_content_equivalent()`: it compares sequences item by item, mappings by keys and values, pydantic models of the same class by their fields, other `Freezable` instances of the same class by their public attributes, and anything else with `==`. It is symmetric, and `Collection` uses it when two models share an id.

## Dumping a frozen model

`BaseModel.model_dump()` on a frozen model warns (`PydanticSerializationUnexpectedValue`) for every field annotated as a list that now holds a tuple. A dict field holding a `frozendict` makes `model_dump(mode="json")` raise `PydanticSerializationError`.

What genanki2 writes does not go through either path. A config blob reaches its row through `ProtobufModel.to_bytes()`, which reads each attribute and hands it to protobuf, and protobuf accepts a tuple as it accepts a list. `Notetype.fields` and `templates` are excluded from the dump, `Note` builds its row itself, and no model has a dict field. The warning appears only when a caller dumps a frozen model directly.

Possible solutions, if a direct dump of a frozen model has to be clean:

- A field serializer per list field, returning a list. The cost is limited to those fields, and each new list field needs its own.
- A wildcard wrap serializer in a mixin placed before `pydantic.BaseModel`, `field_serializer("*", mode="wrap", check_fields=False)`, that turns tuples back into lists and `frozendict`s into dicts before calling the handler. It covers every field at once, but it cannot tell a tuple `deepfreeze()` made from a field annotated as a tuple, and it turns the latter into a list, which moves the warning to that field.
- The same wildcard serializer, passing the value through `pydantic.TypeAdapter(annotation).validate_python()` with the field's annotation before calling the handler. Validation converts each container to what the annotation declares, at any depth, so tuple fields stay tuples. The price is a validation pass on every field of every dump, and validators inside the annotation run again. The `TypeAdapter` has to be cached per annotation.

A JSON dump fails on any model holding a config blob, frozen or not: the blob serializes to bytes, which pydantic encodes as UTF-8. `ser_json_bytes="base64"` in `CheckedModel.model_config` makes it a base64 string. A wrap serializer that returns `to_bytes()` in Python mode and hands the nested model to the handler in JSON mode gives readable JSON instead, at every blob field, and brings the frozen-list warning into JSON mode with it.

## What `deepfreeze()` loses or leaves alone

Conversion keeps values and drops behaviour: a `defaultdict` stops supplying defaults, a `Counter` stops returning 0 for a missing key, a `ChainMap` is flattened, and an `array.array` becomes a tuple of numbers. A tuple subclass other than a namedtuple is left as it is, since nothing says how to rebuild it, and so is a dict view, which stays tied to its dict. A container referenced from two places becomes two separate copies. The buffer under a read-only `memoryview` still changes when written through another reference.

## Private attributes

The guard leaves names beginning with `_` writable, and `is_content_equivalent()` does not compare them, but `freeze()` walks the whole `__dict__`. On a `Freezable` that is not a pydantic model, a list in a private attribute becomes a tuple and the models in it are frozen, while the attribute itself can still be reassigned. A pydantic model keeps private attributes in `__pydantic_private__` and extra attributes in `__pydantic_extra__`, so the freeze reaches neither: an extra attribute cannot be reassigned but a list it holds stays a list. Walking only the public attributes would make the freeze agree with the guard and the comparison.

## Cycles and iterators

The frozen flag is set after the held models are frozen, so two models holding each other raise `RecursionError`, and so does a container that holds itself. Setting the flag before the walk would make cycles safe, but a failure halfway would then leave a model marked frozen over held models the walk never reached. An iterator or generator stored in an attribute is consumed by the search. genanki2's models don't hold these.
