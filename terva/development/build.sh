#!/usr/bin/env bash
# Build one native release image through upstream Bake. Run from the repo root.
set -u
set -o pipefail
fail() { printf '%s\n' "$1" >&2; exit 1; }
root="$(git rev-parse --show-toplevel)" || exit 1
[[ "$PWD" == "$root" ]] || fail 'Run terva/development/build.sh from the repository root.'
git diff --quiet HEAD -- src docker .cargo Cargo.toml Cargo.lock rust-toolchain.toml \
    || fail 'Commit source/build-system changes before building a revision-tagged image.'
untracked="$(git ls-files --others --exclude-standard -- src docker .cargo Cargo.toml Cargo.lock rust-toolchain.toml)" || exit 1
[[ -z "$untracked" ]] || fail 'Commit untracked source/build-system files before building.'
arch="$(docker info --format '{{.Architecture}}')" || exit 1
case "$arch" in
    aarch64|arm64) arch=arm64; triple=aarch64; system=aarch64-v1-linux-gnu ;;
    x86_64|amd64) arch=amd64; triple=x86_64; system=x86_64-v1-linux-gnu ;;
    *) fail 'Only native Linux ARM64 and AMD64 Docker engines are supported.' ;;
esac
revision="$(git rev-parse HEAD)" || exit 1
rust_msrv="$(uv run --no-project python -c 'import tomllib; print(tomllib.load(open("rust-toolchain.toml", "rb"))["toolchain"]["channel"])')" || exit 1
export rust_msrv
export docker_dir=docker cargo_profiles='["release"]' rust_toolchains='["stable"]'
export feat_sets='["default"]' sys_names='["debian"]' sys_versions='["testing-slim"]'
export rust_targets="[\"$triple-unknown-linux-gnu\"]" sys_targets="[\"$system\"]"
export package_revision="$revision"
image="terva/tuwunel-dev:$revision-$arch"
jobs="${JOBS:-4}"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || fail 'JOBS must be a positive integer.'
args=(--provenance=false --allow="fs.read=$root" -f docker/bake.hcl
      --set "*.args.nprocs=$jobs" --set '*.args.sched_policy='
      --set '*.args.cargo_installs=cargo-chef' --set "docker*.tags=$image")
if [[ "${1:-}" == --print && $# == 1 ]]; then
    exec docker buildx bake "${args[@]}" --print docker
fi
[[ $# == 0 ]] || fail 'Usage: terva/development/build.sh [--print]'
docker buildx bake "${args[@]}" docker || exit 1
docker image inspect --format '{{.Id}}' "$image" || exit 1
printf 'Built %s\n' "$image"
