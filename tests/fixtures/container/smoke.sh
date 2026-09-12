#!/usr/bin/env bash
set -euo pipefail
image=$1
platform=$2
test "$(docker image inspect "$image" --format '{{.Config.User}}')" = '65532:65532'
test "$(docker run --rm --platform "$platform" "$image")" = shared-container-fixture
