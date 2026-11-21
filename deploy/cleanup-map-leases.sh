#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repo_dir"

exec docker compose --profile tools run --rm -T jobs cleanup --keep "${MAP_DATASET_KEEP:-2}"
