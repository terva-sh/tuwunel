#!/bin/bash
# Run complement-crypto against tuwunel, without docker bake.
#
# The counterpart of docker/complement-crypto.sh. It hands the run to
# terva/complement/rig/run.sh, which owns the result format and the Complement
# confinement check, and sets what the crypto suite adds.
#
# Usage: CRYPTO_DIR=DIR terva/complement/crypto/run.sh
#
# DIR is a directory made by terva/complement/crypto/prepare.sh. RPC=1 sets
# COMPLEMENT_CRYPTO_RPC_BINARY, which runs the multiprocess restart tests that
# upstream's run skips; leave it unset for a run comparable with upstream's
# tests/complement-crypto/results.jsonl.
#
# RUNTIME picks how the mitmproxy container reaches the callback server this
# process runs:
#   podman (default)  rootless podman on this host, through the rig's
#                     HOST_LOOPBACK service; the server listens on 127.0.0.1.
#   docker            a Docker daemon, with this process in the host network
#                     namespace. The server listens on the default bridge's
#                     gateway, which Docker maps host.docker.internal to. It is
#                     read from the daemon, and TERVA_CALLBACK_BIND, if set,
#                     must match it.
set -u
here="$(cd "$(dirname "$0")" && pwd)"
dir="${CRYPTO_DIR:-}"
src="$dir/complement-crypto"

if [ -z "$dir" ] || [ ! -d "$src/tests" ]; then
    echo "set CRYPTO_DIR to a directory made by terva/complement/crypto/prepare.sh" >&2
    exit 2
fi
# loopback.patch keeps the mitmproxy ports, the callback server and the RPC
# server off the host's other interfaces. Without it the forward proxy and the
# RPC client are open to the LAN, so refuse a harness that lacks any of the
# three, or that builds against an unconfined Complement.
confined=1
grep -qF 'netip.MustParseAddr("127.0.0.1")' "$src/internal/deploy/deploy.go" || confined=0
grep -qF 'TERVA_CALLBACK_BIND' "$src/internal/deploy/callback/callback_addon.go" || confined=0
grep -qF 'net.Listen("tcp", "127.0.0.1:0")' "$src/cmd/rpc/main.go" || confined=0
grep -qF "github.com/matrix-org/complement => $dir/complement" "$src/go.mod" || confined=0
if [ "$confined" -ne 1 ]; then
    echo "$src lacks the loopback patch or the confined Complement; prepare it with $here/prepare.sh" >&2
    exit 2
fi

lib="$dir/matrix-rust-sdk/target/debug"
export LIBRARY_PATH="$lib" LD_LIBRARY_PATH="$lib" CGO_ENABLED=1 CC=clang
export COMPLEMENT_CRYPTO_TEST_CLIENT_MATRIX="${CLIENT_MATRIX:-rr}"
# Cleared otherwise, so an inherited value cannot turn the run compared with
# upstream's results into a multiprocess one.
if [ "${RPC:-0}" = 1 ]; then
    export COMPLEMENT_CRYPTO_RPC_BINARY="$dir/rpc"
else
    unset COMPLEMENT_CRYPTO_RPC_BINARY
fi
# testcontainers starts a Ryuk reaper that publishes its port on every host
# interface. The harness terminates its own containers, so the reaper is not
# needed and is disabled.
export TESTCONTAINERS_RYUK_DISABLED=true
# testcontainers pulls mitmproxy/mitmproxy:10.1.5 on first use. Pull it ahead,
# as docker/complement-crypto.sh does.
mitmproxy=docker.io/mitmproxy/mitmproxy:10.1.5

case "${RUNTIME:-podman}" in
podman)
    podman pull -q "$mitmproxy" > /dev/null || exit 1
    export HOST_LOOPBACK=1
    # Fixed, so an inherited value cannot move the callback server elsewhere.
    export TERVA_CALLBACK_BIND=127.0.0.1
    ;;
docker)
    export DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"
    # The callback server may listen only on the default bridge's gateway, the
    # address host-gateway gives host.docker.internal. Ask the daemon for it
    # rather than trusting a value, which could name a LAN interface.
    sock="${DOCKER_HOST#unix://}"
    gateway="$(curl -fsS --unix-socket "$sock" http://docker/networks/bridge |
        jq -r '.IPAM.Config[0].Gateway // empty')"
    if [ -z "$gateway" ]; then
        echo "could not read the default bridge's gateway from $DOCKER_HOST" >&2
        exit 2
    fi
    if [ -n "${TERVA_CALLBACK_BIND:-}" ] && [ "$TERVA_CALLBACK_BIND" != "$gateway" ]; then
        echo "TERVA_CALLBACK_BIND=$TERVA_CALLBACK_BIND is not the bridge gateway $gateway" >&2
        exit 2
    fi
    export TERVA_CALLBACK_BIND="$gateway"
    export COMPLEMENT_HOSTNAME_RUNNING_COMPLEMENT=host.docker.internal
    export HOST_LOOPBACK=0
    ;;
*)
    echo "RUNTIME must be podman or docker" >&2
    exit 2 ;;
esac

# Verbatim from docker/complement-crypto.sh, which says why each is skipped.
skip="TestOnRejoinBobCanSeeButNotDecryptHistoryInPublicRoom|TestDelayedInviteResponse|TestChangingDeviceAfterInviteReEncrypts"

COMPLEMENT_SRC="$src" COMPLEMENT_LIB_SRC="$dir/complement" \
    TAGS=rust TESTS=./tests SHUFFLE=1337 TIMEOUT=1h \
    SKIP="${SKIP-$skip}" \
    exec "$here/../rig/run.sh"
