#!/usr/bin/env bash
# Run the test suite on every supported Python, each in a throwaway environment built
# from uv.lock, so the project's .venv stays as it is. uv downloads a missing Python.
#
#     scripts/test_all_pythons.sh [pytest arguments]

set -uo pipefail

cd "$(dirname "$0")/.."

failed=()
for version in 3.10 3.11 3.12 3.13 3.14 3.15; do
  echo "=== Python $version"
  uv run --isolated --locked --python "$version" pytest "$@" || failed+=("$version")
done

echo
if ((${#failed[@]})); then
  echo "Failed on: ${failed[*]}"
  exit 1
fi
echo "Passed on every version"
