#!/bin/bash
# Run Complement against tuwunel on this host, without docker bake.
#
# Upstream runs `go test` inside a "tester" container with the docker socket
# mounted, which then spawns homeserver containers. That indirection exists so
# CI gets a hermetic toolchain. Here it buys nothing and costs an image build,
# so this runs go test directly on the host against podman's Docker-compatible
# API. The homeserver containers Complement spawns are identical either way.
#
# The jq pipeline is copied from docker/lib/uwu.sh so results.jsonl comes out in
# exactly the format the committed baseline uses. Deliberately not under
# pipefail, so a failing suite still produces results.

set -u

# COMPLEMENT_SRC must point at a checkout prepared by prepare.sh in this
# directory: the fork this repository pins, with the loopback confinement from
# terva/complement/loopback applied. Unpatched, the harness publishes every
# homeserver port on all host interfaces, so this script refuses it.
SRC="${COMPLEMENT_SRC:-}"
OUT="${COMPLEMENT_OUT:-$PWD/complement-out}"
# A suite that imports Complement as a library, such as complement-crypto, runs
# from its own directory. terva/complement/crypto/run.sh then names the
# prepared Complement checkout that suite builds against here.
LIB="${COMPLEMENT_LIB_SRC:-$SRC}"

if [ -z "$SRC" ] || [ ! -d "$SRC/tests" ]; then
    echo "set COMPLEMENT_SRC to a checkout made by terva/complement/rig/prepare.sh" >&2
    echo "  expected \$COMPLEMENT_SRC/tests to exist" >&2
    exit 2
fi
# The guard file alone proves nothing: the deployer must call it and must no
# longer publish every port. Check the integration prepare.sh applies.
deployer="$LIB/internal/docker/deployer.go"
confined=1
[ -f "$LIB/internal/docker/terva_loopback.go" ] || confined=0
for call in 'tervaLoopbackPorts(' 'tervaCheckRequested(' 'tervaCheckPublished('; do
    grep -qF "$call" "$deployer" 2>/dev/null || confined=0
done
grep -qE 'PublishAllPorts:[[:space:]]*true' "$deployer" 2>/dev/null && confined=0
if [ "$confined" -ne 1 ]; then
    echo "$LIB lacks the loopback confinement; prepare it with terva/complement/rig/prepare.sh" >&2
    exit 2
fi

mkdir -p "$OUT"

# The confinement writes one line per container binding here.
export TERVA_LOOPBACK_OUTPUT="${TERVA_LOOPBACK_OUTPUT:-$OUT/loopback}"
mkdir -p -m 700 "$TERVA_LOOPBACK_OUTPUT"

export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/podman/podman.sock}"

# The appservice suite's callbacks listen on 127.0.0.1, which rootless podman's
# host gateway does not reach. HOST_LOOPBACK=1 runs the suite against a podman
# service of its own whose pasta maps the gateway to the host's loopback. Pasta
# takes that option only when it creates the shared rootless netns, so a netns
# that already exists without it would quietly ignore the setting; refuse that.
if [ "${HOST_LOOPBACK:-0}" = 1 ]; then
    netns="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/containers/networks/rootless-netns"
    # Podman leaves the directory behind empty after a teardown; the namespace
    # is the file inside it.
    if [ -e "$netns/rootless-netns" ] || [ -e "$netns/rootless-netns-conn.pid" ]; then
        pid="$(cat "$netns/rootless-netns-conn.pid" 2>/dev/null)"
        if [ -z "$pid" ] ||
            ! tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null |
            grep -qF -- '--map-host-loopback 169.254.1.3'; then
            echo "a rootless netns exists at $netns without a verifiable loopback map;" >&2
            echo "  wait for the podman containers using it to finish" >&2
            exit 2
        fi
    fi
    sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/terva-complement-$$.sock"
    CONTAINERS_CONF_OVERRIDE="$(cd "$(dirname "$0")" && pwd)/containers-hostloop.conf" \
        podman system service --time=0 "unix://$sock" > "$OUT/podman-service.log" 2>&1 &
    service=$!
    trap 'kill "$service" 2>/dev/null; wait "$service" 2>/dev/null; rm -f "$sock"' EXIT
    for _ in $(seq 100); do [ -S "$sock" ] && break; sleep 0.1; done
    if [ ! -S "$sock" ] || ! kill -0 "$service" 2>/dev/null; then
        echo "the private podman service did not start; its log:" >&2
        cat "$OUT/podman-service.log" >&2
        exit 2
    fi
    export DOCKER_HOST="unix://$sock"
fi
export COMPLEMENT_BASE_IMAGE="${COMPLEMENT_BASE_IMAGE:-complement-testee:latest}"
# Podman maps both names to the host, but be explicit about which runtime we are.
# Docker maps only host.docker.internal, so a Docker run names it.
export COMPLEMENT_HOSTNAME_RUNNING_COMPLEMENT="${COMPLEMENT_HOSTNAME_RUNNING_COMPLEMENT:-host.containers.internal}"
export COMPLEMENT_ALWAYS_PRINT_SERVER_LOGS=0
export COMPLEMENT_DESTROY_HS_TIMEOUT_SECS=10
export COMPLEMENT_SPAWN_HS_TIMEOUT_SECS="${COMPLEMENT_SPAWN_HS_TIMEOUT_SECS:-60}"
# go test runs on the host here, which cannot resolve host.docker.internal, so
# the appservice suite's callback readiness check dials the listener directly.
export TERVA_CALLBACK_READINESS_HOST="${TERVA_CALLBACK_READINESS_HOST:-127.0.0.1}"

# Verbatim from docker/complement.sh. Three are top-level tests and the rest are
# subtests, which is why listing shows exactly three more tests than the
# baseline records.
skip="TestThreadReceiptsInSyncMSC4102"
skip="${skip}|TestToDeviceMessagesOverFederation/stopped_server"
skip="${skip}|TestRestrictedRoomsRemoteJoinFailOver"
skip="${skip}|TestRestrictedRoomsRemoteJoinFailOverInMSC3787Room"
skip="${skip}|TestToDeviceMessagesOverFederation/interrupted_connectivity"
skip="${skip}|TestDeviceListsUpdateOverFederation/stopped_server"
skip="${skip}|TestJumpToDateEndpoint/parallel/federation"
skip="${skip}|TestJumpToDateEndpoint/parallel/should_find_next_event_topologically"
# docker/complemau.sh replaces the list outright, as SKIP does here.
skip="${SKIP-$skip}"

# Upstream's defaults for a homogeneous run: one count, no shuffle, serial.
# docker/complement-crypto.sh shuffles with a fixed seed, so SHUFFLE is settable.
TAGS="${TAGS:-conduwuit_blacklist}"
SHUFFLE="${SHUFFLE:-off}"
PARALLEL="${PARALLEL:-1}"
TIMEOUT="${TIMEOUT:-2h}"
RUN="${RUN:-.*}"
TESTS="${TESTS:-./tests/...}"

jq_res='{Action: .Action, Test: .Test}'
jq_sel='select((.Action == "pass" or .Action == "fail" or .Action == "skip") and .Test != null)'
jq_out='select(.Test != null) | {Test: .Test, Output: .Output}'

echo "==> DOCKER_HOST=$DOCKER_HOST"
echo "==> image=$COMPLEMENT_BASE_IMAGE tags=$TAGS tests=$TESTS parallel=$PARALLEL timeout=$TIMEOUT shuffle=$SHUFFLE"
date

cd "$SRC" || exit 1
go test \
    -json \
    "-shuffle=$SHUFFLE" \
    "-parallel=$PARALLEL" \
    "-timeout=$TIMEOUT" \
    -count=1 \
    "-tags=$TAGS" \
    "-skip=$skip" \
    "-run=$RUN" \
    "$TESTS" |
    jq --unbuffered -c "." |
    tee "$OUT/output.jsonl" |
    jq --unbuffered -c "$jq_sel | $jq_res" |
    tee "$OUT/results_raw.jsonl" |
    jq --unbuffered -r '[.Action, .Test] | @tsv'

# Same normalisation upstream applies before comparing against the baseline.
jq -s -c "sort_by(.Test)[]" < "$OUT/results_raw.jsonl" | uniq > "$OUT/results.jsonl"
jq -s -c "sort_by(.Test)[] | $jq_out" < "$OUT/output.jsonl" > "$OUT/full_output.jsonl"

date
echo "==> results: $OUT/results.jsonl ($(wc -l < "$OUT/results.jsonl") lines)"
