"""Does `zstd_decompress` read the frames both writers produce, and refuse a broken one?

    uv run --with pytest pytest tests/test_zstd_decompress.py

genanki2 compresses in one call, which records the original size in the frame header.
Anki's exporter compresses as a stream (`zstd::stream` in rslib), which leaves it out,
and the one-call `decompress` refuses that frame. `test_anki_package_roundtrip.py`
covers the same failure through a real export.
"""

import sys

import pytest

from genanki2.io_utils import zstd_decompress

if sys.version_info >= (3, 14):
  from compression import zstd  # pyright: ignore[reportUnreachable]
else:
  import zstandard as zstd

# b"streamed by Anki", compressed as a stream: the header carries no content size.
STREAMED = bytes.fromhex("28b52ffd005881000073747265616d656420627920416e6b69")


def test_a_frame_without_its_size_is_read():
  assert zstd_decompress(STREAMED) == b"streamed by Anki"


def test_a_frame_with_its_size_is_read():
  assert (
    zstd_decompress(zstd.compress(b"written by genanki2")) == b"written by genanki2"
  )


def test_a_truncated_frame_is_refused():
  """A short stream decodes to whatever it held without complaint, so the helper has to
  check that the frame ended. The zip's CRC catches a truncated entry, but not a frame
  its writer cut short."""
  with pytest.raises(zstd.ZstdError):
    zstd_decompress(STREAMED[:-3])


def test_data_that_is_not_zstd_is_refused():
  with pytest.raises(zstd.ZstdError):
    zstd_decompress(b"not a zstd frame")
