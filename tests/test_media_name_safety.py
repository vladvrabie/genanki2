"""Does `is_media_name_safe` refuse every name that would land outside the directory?

    uv run --with pytest pytest tests/test_media_name_safety.py

A package is untrusted input and its media map holds names the reader joins to the
directory it extracts into, so a name is an instruction about where to write a file.
`../../.bashrc`, `/etc/cron.d/anki` and, on Windows, `C:evil.exe` are all names a zip
can carry, and joining any of them to a chosen directory writes somewhere else.

The check runs before `normalize_media_name()` and answers a narrower question: not
whether the name is well formed, but whether it names one file inside the directory.
The two are kept apart so that an escape is refused and a merely awkward name is
repaired.

Anki's rule is the same one and lives in `filename_is_safe` (`rslib/io/src/lib.rs`):
exactly one path component, and a normal one. genanki2 applies both platforms' rules at
once, so a package written on Windows and read on Linux gets the same answer.
"""

import ntpath
import posixpath

import pytest

from genanki2.text import is_media_name_safe

# A name is judged on where it would write, not on how well formed it is: every one of
# these still has to survive `normalize_media_name()` before it reaches a filesystem.
SAFE = [
  "hello.mp3",
  "_style.css",
  "café.mp3",
  "...",
  "con.mp3",  # a device name, repaired later, but it names one file here
  "trailing period.",
  "trailing space ",
  "a&amp;b.jpg",  # the spelling a note field uses, entities and all
  "bell\x07.mp3",
]

# Refused because at least one platform writes them somewhere other than the directory.
ESCAPES = [
  "",
  ".",
  "..",
  "/",
  "\\",
  "/etc/cron.d/anki",
  "\\windows\\system32\\a.dll",
  "../../.bashrc",
  "a/../../b.mp3",
  "a\\b.mp3",  # a subdirectory on Windows, a filename on Linux
  "\\\\server\\share\\a.mp3",
  "C:",
  "C:evil.exe",  # drive-relative: no separator, and it still escapes
  "a:b.mp3",  # any single character before a colon is a drive letter
  "C:\\evil.exe",
  "c:x",
]

# Refused although both platforms would write them where they belong.
HARMLESS_BUT_REFUSED = [
  "./a.mp3",  # Anki refuses it too: the leading `.` is a component of its own
  "a.mp3/",  # a name carrying a separator is a path, not a file name
]


@pytest.mark.parametrize("name", SAFE)
def test_a_name_of_one_file_is_safe(name: str):
  assert is_media_name_safe(name)


@pytest.mark.parametrize("name", ESCAPES + HARMLESS_BUT_REFUSED)
def test_a_name_that_is_not_one_file_is_refused(name: str):
  assert not is_media_name_safe(name)


@pytest.mark.parametrize("name", SAFE)
def test_a_safe_name_writes_directly_into_the_directory(name: str):
  """The property the check exists for, on both platforms' path rules."""
  for path, base in ((posixpath, "/out"), (ntpath, "D:\\out")):
    assert path.dirname(path.normpath(path.join(base, name))) == base


@pytest.mark.parametrize("name", ESCAPES)
def test_a_refused_name_writes_somewhere_else_on_at_least_one_platform(name: str):
  """Which is why both platforms' rules are applied wherever genanki2 runs.

  Half of these are ordinary filenames on Linux and escapes on Windows, and a package
  carrying one arrives from whichever platform wrote it.
  """
  assert any(
    path.dirname(path.normpath(path.join(base, name))) != base
    for path, base in ((posixpath, "/out"), (ntpath, "D:\\out"))
  )
