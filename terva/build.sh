#!/bin/bash
# Build tuwunel inside a container.
#
# The host this fork is developed on cannot install clang or libclang-dev, which
# RocksDB's bindgen step requires, and the `docker` command here is a podman
# shim that does not implement `docker buildx bake`. So the project's own build
# system in docker/ does not run. This script is the fallback: one container
# image with the same packages bake installs, and a cargo invocation inside it.
#
# The repository is mounted read only. Build output goes to a cache directory
# outside the tree, so the working tree stays clean and `git status` stays
# readable. Cargo runs with --locked, so Cargo.lock is never rewritten.
#
# Usage:
#   terva/build.sh                      # release build matching upstream's default
#   terva/build.sh check                # cargo check
#   terva/build.sh clippy -- -D warnings
#   terva/build.sh test --workspace
#
# The first argument is the cargo subcommand and defaults to `build`. Everything
# after it is passed to cargo unchanged. Passing any argument to `build`
# suppresses the defaults below, so you drive the feature set yourself.
#
# A bare `build` adds `-p tuwunel --features direct_tls` on top of --release.
# That is not decoration. `docker/bake.hcl` sets
# `cargo_features_always = "direct_tls"`, so every binary upstream builds has
# TLS whichever feature set it names, while a plain `cargo build` produces one
# that cannot serve HTTPS and so cannot federate. Without this the script would
# quietly hand you a different binary than CI tests, which is worse than having
# no build script at all. `-p tuwunel` is required because --features is not
# allowed in the root of a virtual workspace.
#
# IMAGE, CACHE, and JOBS override the image tag, the cache directory, and the
# parallelism.

set -euo pipefail

cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-localhost/terva/tuwunel-build:latest}"
CACHE="${CACHE:-$HOME/.cache/tuwunel-ctr}"
JOBS="${JOBS:-$(nproc)}"

subcommand="${1:-build}"
shift || true

if [ "$subcommand" = "build" ] && [ $# -eq 0 ]; then
    set -- --release -p tuwunel --features direct_tls
fi

mkdir -p "$CACHE/target" "$CACHE/cargo"

if ! podman image exists "$IMAGE"; then
    echo "==> building $IMAGE"
    podman build -t "$IMAGE" -f terva/Containerfile.build .
fi

echo "==> cargo $subcommand $* (jobs=$JOBS)"
exec podman run --rm \
    -v "$PWD:/work:ro" \
    -v "$CACHE/target:/target" \
    -v "$CACHE/cargo:/cargo" \
    -w /work \
    -e CARGO_TARGET_DIR=/target \
    -e CARGO_HOME=/cargo \
    -e CARGO_BUILD_JOBS="$JOBS" \
    -e RUMA_UNSTABLE_EXHAUSTIVE_TYPES=true \
    "$IMAGE" \
    cargo "$subcommand" --locked "$@"
