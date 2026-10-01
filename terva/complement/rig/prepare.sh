#!/bin/bash
# Prepare a Complement checkout for terva/complement/rig/run.sh.
#
# Clones the pinned fork into DIR and applies the loopback confinement from
# terva/complement/loopback, the same steps Containerfile.loopback runs inside
# the Docker tester image. Without it the harness publishes homeserver ports on
# every host interface. run.sh refuses a checkout this script did not prepare.
#
# Usage: terva/complement/rig/prepare.sh DIR
#
# DIR must not exist, and must sit outside this repository. Needs git and a Go
# toolchain on the host.

set -euo pipefail

cd "$(dirname "$0")/../../.."

dir="${1:-}"
if [ -z "$dir" ]; then
    echo "usage: $0 DIR" >&2
    exit 2
fi
if [ -e "$dir" ]; then
    echo "$dir already exists; choose a new directory" >&2
    exit 2
fi
dir="$(realpath -m "$dir")"
case "$dir/" in "$PWD"/*)
    echo "$dir is inside this repository; choose a directory outside it" >&2
    exit 2 ;;
esac

# The ref is the complement_ref ARG in docker/Dockerfile.complement.
ref="$(sed -n 's/^ARG complement_ref="\([0-9a-f]\{40\}\)"$/\1/p' docker/Dockerfile.complement)"
if [ -z "$ref" ]; then
    echo "could not read complement_ref from docker/Dockerfile.complement" >&2
    exit 2
fi

loopback="$PWD/terva/complement/loopback"
git clone -q https://github.com/matrix-construct/complement.git "$dir"
git -C "$dir" checkout -q "$ref"

cd "$dir"

# apply.go checks the SHA-256 of every file it patches before writing any.
go run "$loopback/apply.go"
cp "$loopback"/terva_loopback*.go internal/docker/
cp "$loopback"/terva_callback*.go tests/complemau/
gofmt -w internal/docker/deployer.go internal/docker/terva_loopback*.go tests/complemau/terva_callback*.go
go test ./internal/docker -run '^TestTervaLoopbackPolicy$' -count=1

echo "prepared $PWD at $ref"
