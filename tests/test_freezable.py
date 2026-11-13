"""Does a frozen instance refuse changes, and does the freeze reach every model it holds?

    uv run --with pytest pytest tests/test_freezable.py

The builtins are shared by every note in the process, so a freeze that misses a model
held two containers down leaves that model open to the edit it exists to stop. The
search tests below place a model in each kind of container `nested_instances` enters,
and `tests/test_utils.py` tests the search itself.

The freeze is also invisible to comparison and serialization, since `_collect` compares
a frozen builtin with an unfrozen copy of it.
"""

import collections
import types
import typing

import pydantic as pyd
import pytest
from frozendict import frozendict
from pydantic_core import PydanticSerializationError

from genanki2.freezable import (
  DeepFreezable,
  Freezable,
  _is_content_equivalent,
  deepfreeze,
)


class Model(Freezable, pyd.BaseModel):
  model_config = pyd.ConfigDict(validate_assignment=True)

  name: str = "leaf"
  kids: list["Model"] = []
  by_name: dict[str, list["Model"]] = {}


class Plain(pyd.BaseModel):
  """A pydantic model that is not `Freezable`, searched through its fields."""

  kid: Model


class Box(Freezable):
  """A plain `Freezable`: hashable by identity, so it can be a mapping key."""

  def __init__(self, *items: object) -> None:
    self.items = items


class ValuesOnly:
  """Has `values()` and nothing else of the mapping protocol, and is not iterable."""

  def __init__(self, *values: object) -> None:
    self._values = values

  def values(self) -> tuple[object, ...]:
    return self._values


# ----------------------------------------- The guard ----------------------------------------


def test_a_frozen_instance_refuses_assignment():
  model = Model().freeze()
  with pytest.raises(AttributeError, match="Model is frozen"):
    model.name = "changed"
  assert model.name == "leaf"


def test_a_frozen_instance_refuses_deletion():
  model = Model().freeze()
  with pytest.raises(AttributeError, match="Model is frozen"):
    del model.name
  assert model.name == "leaf"


def test_a_private_name_stays_writable():
  model = Model().freeze()
  model._scratch = 1
  del model._scratch


def test_other_instances_of_the_class_stay_writable():
  Model().freeze()
  model = Model()
  model.name = "changed"
  assert model.name == "changed"


def test_an_unfrozen_instance_still_validates_assignment():
  """The guard sits in front of pydantic and hands a writable instance on to it."""
  model = Model()
  with pytest.raises(pyd.ValidationError):
    model.name = 1  # pyright: ignore[reportAttributeAccessIssue]


def test_freeze_returns_the_instance_and_can_be_repeated():
  model = Model()
  assert model.freeze() is model
  assert model.freeze() is model


def test_the_freeze_leaves_the_containers_open():
  """Closing them is `DeepFreezable`'s job."""
  model = Model().freeze()
  model.kids.append(Model())
  assert len(model.kids) == 1


def test_a_frozen_instance_equals_an_unfrozen_twin():
  frozen = Model(kids=[Model()]).freeze()
  assert frozen == Model(kids=[Model()])


def test_the_flag_stays_out_of_the_dump():
  model = Model(kids=[Model()])
  unfrozen = model.model_dump_json()
  assert model.freeze().model_dump_json() == unfrozen


# ---------------------------------------- The search ----------------------------------------


def test_the_freeze_reaches_a_model_in_a_list():
  kid = Model()
  Model(kids=[kid]).freeze()
  assert kid._frozen


def test_the_freeze_reaches_a_model_in_a_dict_of_lists():
  kid = Model()
  Model(by_name={"a": [kid]}).freeze()
  assert kid._frozen


def test_the_freeze_reaches_every_level():
  grandchild = Model()
  Model(kids=[Model(kids=[grandchild])]).freeze()
  assert grandchild._frozen


@pytest.mark.parametrize(
  "container",
  [
    pytest.param(lambda box: [[[box]]], id="nested lists"),
    pytest.param(lambda box: (box,), id="tuple"),
    pytest.param(lambda box: {box}, id="set"),
    pytest.param(lambda box: {"key": box}, id="dict value"),
    pytest.param(lambda box: {box: "value"}, id="dict key"),
    pytest.param(lambda box: ValuesOnly(box), id="values() only"),
  ],
)
def test_the_freeze_reaches_a_model_in(container):
  box = Box()
  Box(container(box)).freeze()
  assert box._frozen


def test_the_freeze_reaches_a_model_inside_a_pydantic_model_that_is_not_freezable():
  kid = Model()
  Box(Plain(kid=kid)).freeze()
  assert kid._frozen


# ----------------------------------------- The limits ---------------------------------------


def test_the_search_consumes_an_iterator():
  stream = iter([Model()])
  Box(stream).freeze()
  assert list(stream) == []


def test_two_instances_holding_each_other_raise():
  first, second = Box(), Box()
  first.items, second.items = (second,), (first,)
  with pytest.raises(RecursionError):
    first.freeze()


# ---------------------------------------- deepfreeze ----------------------------------------

Pair = collections.namedtuple("Pair", "key value")


class TypedPair(typing.NamedTuple):
  key: str
  value: list[int]


@pytest.mark.parametrize(
  ("mutable", "frozen"),
  [
    pytest.param([1, 2], (1, 2), id="list"),
    pytest.param(collections.deque([1, 2]), (1, 2), id="deque"),
    pytest.param({"a": 1}, frozendict(a=1), id="dict"),
    pytest.param(collections.OrderedDict(a=1), frozendict(a=1), id="OrderedDict"),
    pytest.param(collections.defaultdict(int, a=1), frozendict(a=1), id="defaultdict"),
    pytest.param(types.MappingProxyType({"a": 1}), frozendict(a=1), id="mappingproxy"),
    pytest.param({1, 2}, frozenset({1, 2}), id="set"),
    pytest.param(bytearray(b"ab"), b"ab", id="bytearray"),
  ],
)
def test_a_mutable_container_becomes_its_immutable_counterpart(mutable, frozen):
  result = deepfreeze(mutable)
  assert type(result) is type(frozen)
  assert result == frozen


def test_the_conversion_reaches_every_level():
  result = deepfreeze({"a": [{"b": [1]}, {2}]})
  assert repr(result) == repr(frozendict(a=(frozendict(b=(1,)), frozenset({2}))))


@pytest.mark.parametrize(
  "immutable",
  [
    pytest.param((1, [2]), id="tuple"),
    pytest.param(frozendict(a=[2]), id="frozendict"),
  ],
)
def test_the_conversion_enters_an_immutable_container(immutable):
  """A tuple holding a list is immutable only at its own level."""
  result = deepfreeze(immutable)
  assert type(result) is type(immutable)
  assert (2,) in tuple(result.values() if isinstance(result, frozendict) else result)


@pytest.mark.parametrize(
  "pair",
  [
    pytest.param(Pair("a", [1]), id="collections.namedtuple"),
    pytest.param(TypedPair("a", [1]), id="typing.NamedTuple"),
  ],
)
def test_a_namedtuple_keeps_its_class_and_freezes_its_items(pair):
  result = deepfreeze(pair)
  assert type(result) is type(pair)
  assert result.value == (1,)


def test_a_memoryview_becomes_read_only():
  buffer = bytearray(b"ab")
  view = deepfreeze(memoryview(buffer))
  assert view.readonly
  with pytest.raises(TypeError):
    view[0] = 0


@pytest.mark.parametrize(
  "leaf",
  [
    pytest.param("text", id="str"),
    pytest.param(b"bytes", id="bytes"),
    pytest.param(collections.UserString("text"), id="UserString"),
    pytest.param(range(3), id="range"),
    pytest.param(1, id="int"),
    pytest.param(None, id="None"),
    pytest.param(frozenset({(1, 2)}), id="frozenset"),
  ],
)
def test_an_immutable_value_comes_back_unchanged(leaf):
  assert deepfreeze(leaf) is leaf


def test_text_inside_a_container_is_not_iterated():
  text, user_text = "ab", collections.UserString("ab")
  result = deepfreeze([text, user_text])
  assert result[0] is text and result[1] is user_text


def test_mapping_keys_are_kept_as_they_are():
  key = (1, 2)
  [result_key] = deepfreeze({key: [1]})
  assert result_key is key


def test_a_freezable_model_is_left_to_its_own_freeze():
  model = Model()
  [result] = deepfreeze([model])
  assert result is model
  assert not model._frozen


def test_a_dict_view_is_returned_live():
  """A view stays tied to its dict, which the conversion never touches."""
  mapping = {"a": 1}
  keys = deepfreeze(mapping.keys())
  mapping["b"] = 2
  assert set(keys) == {"a", "b"}


def test_an_items_view_holding_lists_is_returned_as_it_is():
  """As a set, its pairs would be hashed, and a pair holding a list cannot be."""
  items = {"a": [1]}.items()
  assert deepfreeze(items) is items


class Tagged(tuple):
  """A tuple subclass that is not a namedtuple: nothing says how to rebuild it."""


def test_a_tuple_subclass_that_is_not_a_namedtuple_is_not_entered():
  tagged = Tagged((1, [2]))
  result = deepfreeze(tagged)
  assert result is tagged
  assert type(result[1]) is list


def test_a_container_holding_itself_raises():
  loop: list[object] = []
  loop.append(loop)
  with pytest.raises(RecursionError):
    deepfreeze(loop)


# --------------------------------------- DeepFreezable --------------------------------------


class DeepModel(DeepFreezable, pyd.BaseModel):
  model_config = pyd.ConfigDict(validate_assignment=True)

  name: str = "leaf"
  steps: list[float] = [1.0]
  kids: list["DeepModel"] = []
  shallow_kids: list[Model] = []


class DeepMapping(DeepFreezable, pyd.BaseModel):
  by_name: dict[str, list[int]] = {"a": [1]}


def test_a_deep_frozen_list_refuses_append():
  model = DeepModel().freeze()
  assert type(model.steps) is tuple
  with pytest.raises(AttributeError):
    model.steps.append(2.0)


def test_a_deep_frozen_dict_refuses_item_assignment():
  model = DeepMapping().freeze()
  assert type(model.by_name) is frozendict
  assert model.by_name["a"] == (1,)
  with pytest.raises(TypeError):
    model.by_name["b"] = [2]


def test_a_deep_frozen_instance_still_refuses_assignment():
  model = DeepModel().freeze()
  with pytest.raises(AttributeError, match="DeepModel is frozen"):
    model.name = "changed"


def test_a_deep_freezable_model_in_a_list_is_deep_frozen():
  kid = DeepModel()
  DeepModel(kids=[kid]).freeze()
  assert kid._frozen
  assert type(kid.steps) is tuple


def test_a_freezable_model_in_a_list_is_frozen_and_keeps_its_lists():
  """A model that is only `Freezable` gets the shallow freeze, wherever it is held."""
  kid = Model()
  DeepModel(shallow_kids=[kid]).freeze()
  assert kid._frozen
  assert type(kid.kids) is list


def test_a_second_deep_freeze_changes_nothing():
  model = DeepModel().freeze()
  steps = model.steps
  model.freeze()
  assert model.steps is steps


def test_an_unfrozen_instance_keeps_its_lists():
  model = DeepModel()
  model.steps.append(2.0)
  model.steps = (3.0,)  # pyright: ignore[reportAttributeAccessIssue]
  assert model.steps == [3.0]


def test_a_deep_frozen_instance_differs_from_an_unfrozen_twin():
  """A list does not equal a tuple, so `==` reads the swap as a change of content."""
  assert DeepModel().freeze() != DeepModel()


def test_dumping_a_deep_frozen_list_field_warns():
  model = DeepModel().freeze()
  with pytest.warns(UserWarning, match="serializer warnings"):
    model.model_dump()


@pytest.mark.skipif(
  issubclass(frozendict, dict), reason="frozendict runs as its pure Python class"
)
def test_a_json_dump_of_a_deep_frozen_dict_field_raises_on_the_c_extension():
  """The C extension is not a `dict` subclass, so pydantic has no serializer for it."""
  model = DeepMapping().freeze()
  with pytest.raises(PydanticSerializationError, match="frozendict"):
    model.model_dump(mode="json")


@pytest.mark.skipif(
  not issubclass(frozendict, dict), reason="frozendict runs as its C extension"
)
def test_a_json_dump_of_a_deep_frozen_dict_field_works_on_the_pure_python_class():
  """The pure Python class is a `dict` subclass, so only the frozen list warns."""
  model = DeepMapping().freeze()
  with pytest.warns(UserWarning, match="serializer warnings"):
    assert model.model_dump(mode="json") == {"by_name": {"a": [1]}}


# ------------------------------------ Content equivalence -----------------------------------


class DeepModelCopy(DeepModel):
  """Same fields as `DeepModel`, another class."""


class WithPrivate(DeepFreezable, pyd.BaseModel):
  model_config = pyd.ConfigDict(extra="allow")

  steps: list[float] = [1.0]
  _source: bytes = pyd.PrivateAttr(default=b"")


class ShallowHolder(Freezable, pyd.BaseModel):
  kid: DeepModel


class Record(DeepFreezable):
  """A `DeepFreezable` that is not a pydantic model."""

  def __init__(self, steps: list[int], note: str = "") -> None:
    self.steps = steps
    self._note = note


@pytest.mark.parametrize(
  ("a", "b", "equivalent"),
  [
    pytest.param([1, [2]], (1, (2,)), True, id="list and tuple, nested"),
    pytest.param([1, 2], (1,), False, id="sequences of different lengths"),
    pytest.param([1, 2], (2, 1), False, id="sequences in another order"),
    pytest.param({"a": [1]}, frozendict(a=(1,)), True, id="dict and frozendict"),
    pytest.param({"a": 1}, {"b": 1}, False, id="mappings with different keys"),
    pytest.param({"a": 1}, {"a": 2}, False, id="mappings with different values"),
    pytest.param({"_a": 1}, {"_a": 2}, False, id="a mapping key beginning with _"),
    pytest.param({1, 2}, frozenset({1, 2}), True, id="set and frozenset"),
    pytest.param(b"ab", bytearray(b"ab"), True, id="bytes and bytearray"),
    pytest.param("ab", ["a", "b"], False, id="a string and its characters"),
    pytest.param(["a", "b"], "ab", False, id="characters and their string"),
    pytest.param(
      collections.UserString("ab"), collections.UserString("ab"), True, id="UserString"
    ),
    pytest.param(collections.UserString("ab"), "ab", True, id="UserString and str"),
    pytest.param(Pair("a", [1]), ("a", (1,)), True, id="namedtuple and tuple"),
    pytest.param(object(), object(), False, id="objects without content"),
  ],
)
def test_content_equivalence_of(a, b, equivalent):
  assert _is_content_equivalent(a, b) is equivalent
  assert _is_content_equivalent(b, a) is equivalent


def test_a_frozen_instance_is_equivalent_to_an_unfrozen_twin():
  frozen = DeepModel(kids=[DeepModel()]).freeze()
  unfrozen = DeepModel(kids=[DeepModel()])
  assert frozen != unfrozen
  assert frozen.is_content_equivalent(unfrozen)
  assert unfrozen.is_content_equivalent(frozen)


def test_a_different_field_breaks_the_equivalence():
  frozen = DeepModel(kids=[DeepModel(steps=[2.0])]).freeze()
  assert not frozen.is_content_equivalent(DeepModel(kids=[DeepModel()]))


def test_models_of_different_classes_are_not_equivalent():
  assert not DeepModel().is_content_equivalent(DeepModelCopy())
  assert not DeepModelCopy().is_content_equivalent(DeepModel())


def test_private_and_extra_attributes_are_not_content():
  a = WithPrivate(note="a").freeze()  # pyright: ignore[reportCallIssue]
  b = WithPrivate(note="b")  # pyright: ignore[reportCallIssue]
  b._source = b"read from a file"
  assert a.is_content_equivalent(b)


def test_a_freezable_model_holding_a_frozen_one_is_equivalent():
  frozen = ShallowHolder(kid=DeepModel()).freeze()
  assert frozen.kid._frozen
  assert _is_content_equivalent(frozen, ShallowHolder(kid=DeepModel()))


def test_a_deep_freezable_that_is_not_a_model_compares_its_public_attributes():
  assert Record([1], note="a").freeze().is_content_equivalent(Record([1], note="b"))
  assert not Record([1]).freeze().is_content_equivalent(Record([2]))


def test_a_freezable_that_is_not_a_model_compares_its_public_attributes():
  assert _is_content_equivalent(Box([1]).freeze(), Box((1,)))
  assert not _is_content_equivalent(Box([1]), Box([2]))


def test_freezables_of_different_classes_are_not_equivalent():
  class OtherBox(Box):
    pass

  assert not _is_content_equivalent(Box(1), OtherBox(1))
  assert not _is_content_equivalent(OtherBox(1), Box(1))


def test_comparing_containers_that_hold_themselves_raises():
  a: list[object] = []
  a.append(a)
  b: list[object] = []
  b.append(b)
  with pytest.raises(RecursionError):
    _is_content_equivalent(a, b)
