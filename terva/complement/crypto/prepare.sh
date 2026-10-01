#!/bin/bash
# Prepare complement-crypto for terva/complement/crypto/run.sh.
#
# The host-side counterpart of the complement-crypto-deps and -base stages in
# docker/Dockerfile.complement-crypto. Into DIR it puts:
#   complement/          Complement prepared by terva/complement/rig/prepare.sh
#   complement-crypto/   the pinned harness, built against complement/, with
#                        loopback.patch applied
#   matrix-rust-sdk/     the pinned SDK, built as matrix-sdk-ffi (debug)
#   tools/bin/           uniffi-bindgen-go
#   rpc                  the multiprocess client, for the restart tests
#
# Usage: terva/complement/crypto/prepare.sh DIR
#
# DIR must not exist, and must sit outside this repository. Needs git, Go, a
# rustup toolchain and clang on the host. The FFI build is the expensive part:
# JOBS (default 2) caps cargo's parallelism for a small host.
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

# The pins are the Dockerfile's, so an import that moves them moves this too.
dockerfile=docker/Dockerfile.complement-crypto
arg() { sed -n "s/^ARG $1=\"\([0-9a-f]\{40\}\)\"$/\1/p" "$dockerfile"; }
crypto_ref="$(arg complement_crypto_ref)"
mrsdk_ref="$(arg mrsdk_ref)"
bindgen_tag="$(sed -n 's/^ *--tag \(v[^ ]*\) *\\$/\1/p' "$dockerfile")"
if [ -z "$crypto_ref" ] || [ -z "$mrsdk_ref" ] || [ -z "$bindgen_tag" ]; then
    echo "could not read the pins from $dockerfile" >&2
    exit 2
fi
toolchain="$(sed -n 's/^channel = "\(.*\)"$/\1/p' rust-toolchain.toml)"
here="$PWD/terva/complement/crypto"

mkdir -p "$dir/tmp"
export TMPDIR="$dir/tmp"
export CARGO_BUILD_JOBS="${JOBS:-2}"
export RUSTUP_TOOLCHAIN="$toolchain"
# The kitchen image ships clang and no gcc; cgo gets the same compiler here.
export CGO_ENABLED=1 CC=clang

terva/complement/rig/prepare.sh "$dir/complement"

git clone -q https://github.com/matrix-construct/complement-crypto.git "$dir/complement-crypto"
git -C "$dir/complement-crypto" checkout -q "$crypto_ref"
git clone -q --filter=blob:none https://github.com/matrix-construct/matrix-rust-sdk.git "$dir/matrix-rust-sdk"
git -C "$dir/matrix-rust-sdk" checkout -q "$mrsdk_ref"

cd "$dir/complement-crypto"
# Refuse to patch files that moved under us; the patch was written against these.
sha256sum -c --quiet <<EOF
68a6c6de7398206219ba83bff1ce0fe76bb69bea498dd997bee32d8f54400b03  internal/deploy/deploy.go
fd1a1bad79438b5e32f012e8193cc80f1ddb1034bf7d1d6c728ff79fb089ffed  internal/deploy/callback/callback_addon.go
0f6df10cfbe546e7a6f9ee546d8ecd29dab69ccf97cf59020b24e315a0faa9e6  cmd/rpc/main.go
EOF
git apply "$here/loopback.patch"
# Build against the confined Complement rather than the fork go.mod names.
go mod edit -replace "github.com/matrix-org/complement=$dir/complement"

# --debug, where the Dockerfile builds release: the release build of the
# uniffi-bindgen-go crate needed more than 4 GiB of RAM and 2 GiB of swap and
# was OOM-killed on 2026-09-24. It is a code generator, and the bindings it
# writes do not depend on how it was optimised.
cargo install -q --debug uniffi-bindgen-go --root "$dir/tools" \
    --git https://github.com/NordSecurity/uniffi-bindgen-go --tag "$bindgen_tag"
PATH="$dir/tools/bin:$PATH" ./rebuild_rust_sdk.sh "$dir/matrix-rust-sdk"

export LIBRARY_PATH="$dir/matrix-rust-sdk/target/debug" LD_LIBRARY_PATH="$dir/matrix-rust-sdk/target/debug"
go mod tidy
go build -tags=rust -o "$dir/rpc" ./cmd/rpc
# The Dockerfile's compile check: list the tests with only the rust client built.
# TestMain reads Complement's configuration even when listing, so it needs an
# image name, as the Dockerfile's ENV gives it; nothing is deployed.
if ! list="$(COMPLEMENT_BASE_IMAGE=complement-testee COMPLEMENT_CRYPTO_TEST_CLIENT_MATRIX=rr \
    go test -tags=rust -list='.*' ./tests 2>&1)"; then
    echo "$list" >&2
    exit 1
fi
echo "$(grep -c '^Test' <<< "$list") tests listed"
echo "prepared $dir: complement-crypto $crypto_ref, matrix-rust-sdk $mrsdk_ref, toolchain $toolchain"
