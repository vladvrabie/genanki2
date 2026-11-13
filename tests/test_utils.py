"""Does `nested_instances` find every instance a walk over held models needs?

    uv run --with pytest pytest tests/test_utils.py

`Freezable.freeze()` and the phases of `CheckedModel` both reach the models an instance
holds through it, so a container the search does not enter hides a model from both.
"""

import types
from typing import Any

import pydantic as pyd
import pytest

from genanki2.utils import nested_instances


class Leaf:
  """The type searched for."""


class Bag(list[object]):
  """A list that is also the type searched for, so the search could enter it."""


class Plain(pyd.BaseModel):
  """A pydantic model that is not the type searched for, searched through its fields."""

  kid: Any


# ------------------------------------- nested_instances -------------------------------------


def test_the_search_enters_a_pydantic_model_that_is_not_searched_for():
  leaf = Leaf()
  assert list(nested_instances([Plain(kid=leaf)], Leaf)) == [leaf]


def test_the_search_stops_at_an_instance_it_finds():
  """What the instance holds is left to whoever searches it next."""
  outer = Bag([Bag()])
  assert list(nested_instances([outer], Bag)) == [outer]


def test_the_search_yields_an_instance_each_time_it_is_reached():
  leaf = Leaf()
  assert list(nested_instances([[leaf], {"a": leaf}], Leaf)) == [leaf, leaf]


@pytest.mark.parametrize("searched", [1, None, "text", b"bytes", ["a", b"b", 1]])
def test_the_search_finds_nothing_in(searched):
  assert list(nested_instances(searched, Leaf)) == []


def test_the_search_leaves_a_values_attribute_that_is_not_a_method():
  """Calling it would raise `TypeError`: 3 is not callable."""
  assert list(nested_instances([types.SimpleNamespace(values=3)], Leaf)) == []
