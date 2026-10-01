# Running complement-crypto without bake

Upstream runs its real-client encryption suite with `docker/complement-crypto.sh`,
which builds the tester through Docker Bake. It builds matrix-sdk-ffi and its
Go bindings, then runs complement-crypto's Go tests against two homeservers
behind a mitmproxy container. The committed results are
`tests/complement-crypto/results.jsonl`. This directory builds the same pieces
without Bake and runs them through `terva/complement/rig/run.sh`, so a run here
produces results in the same format and passes the same confinement check.

## What is here

- `prepare.sh DIR` builds everything into a new directory outside the
  repository. It reads the pins from `docker/Dockerfile.complement-crypto`:
  - Complement, prepared by `terva/complement/rig/prepare.sh`.
  - complement-crypto, with `loopback.patch` applied. It is built against
    that Complement rather than the fork its `go.mod` names.
  - matrix-sdk-ffi, a debug build made by complement-crypto's own
    `rebuild_rust_sdk.sh`.
  - uniffi-bindgen-go.
  - The `rpc` client binary.
- `run.sh` runs the suite with the arguments `docker/complement-crypto.sh`
  uses: the `rr` client matrix, tag `rust`, `./tests`, shuffle seed 1337, a
  one-hour timeout and its skip list.
- `loopback.patch` keeps the harness's own listeners off the host's other
  interfaces. `run.sh` refuses a harness without it.
- `Containerfile.tools` is a toolchain image for a host that has Docker and
  not the tools. It holds Rust, Go, clang, curl and jq, and no sources.

## What the patch closes

Complement's own ports are confined by `terva/complement/loopback`. As upstream
ships it, complement-crypto adds four more listeners, and each one binds every
host interface:

- **mitmproxy.** `internal/deploy/deploy.go` publishes the container's three
  ports on every interface. One is a forward proxy (`--mode regular`) that
  anyone on the LAN could use. The patch binds all three to `127.0.0.1`.
- **The callback server.** `internal/deploy/callback` listens on `:0` for
  calls from the mitmproxy addon. The patch binds it to `TERVA_CALLBACK_BIND`,
  which defaults to `127.0.0.1` and must be one specific address.
- **The RPC server.** `cmd/rpc` listens on `:0` and drives a live Matrix
  client for its parent process, which dials `127.0.0.1`. The patch binds it
  there.
- **The Ryuk reaper.** testcontainers starts it with a published port.
  `run.sh` disables it; the harness removes its own containers.

## The restart tests

`TestUnprocessedToDeviceMessagesArentLostOnRestart`,
`TestRoomKeyIsNotCycledOnClientRestart` and `TestSigkillBeforeKeysUploadResponse`
run their clients as separate processes. Their `/rust` subtests skip unless
`COMPLEMENT_CRYPTO_RPC_BINARY` names the `rpc` binary
(`internal/cc/test_context.go`). Upstream's runner never sets it, so upstream's
results record them as skipped. `RPC=1` sets it. Keep that run separate from
the one compared with upstream's results.

## With Docker, on a host without the tools

Tried on 2026-09-24 on a host with Debian 13, 4 cores, 7.6 GiB of RAM, Docker 29.
The build did not fit. One crate, `uniffi_bindgen_go`, needs more than 6 GiB to
compile, in release or debug and on 1.95.0 or nightly. The container ran under
a 4 GiB limit plus 2 GiB of swap, and that crate was killed there every time.
So give the build at least 8 GiB, and more when other work shares the host.
The toolchain image and the steps up to the Complement checkout ran there. The
build and run commands below have not been run to completion anywhere.

Put the repository's `terva/complement`, `docker/Dockerfile.complement`,
`docker/Dockerfile.complement-crypto` and `rust-toolchain.toml` under
`$W/repo`. Load a testee image as `terva/complement/rig/README.md` builds it.
Then run the steps below. `W` must be the same path inside the container as on
the host, because testcontainers bind-mounts the addons directory through the
host's daemon. `Containerfile.tools` pulls its base images from Docker Hub;
pass `--build-arg REGISTRY=...` to use a pull-through mirror of
`docker.io/library` instead.

```sh
W=$HOME/complement-crypto
docker build \
    -t terva-crypto-tools:1 -f $W/repo/terva/complement/crypto/Containerfile.tools \
    $W/repo/terva/complement/crypto
docker pull docker.io/mitmproxy/mitmproxy:10.1.5
mkdir -p $W/home $W/cargo

# Build. JOBS caps cargo, and the container limit keeps it off the host's other
# work. MEM must leave the build room: 4 GiB with 2 GiB of swap failed, and the
# podman build below peaked at 11 GiB with JOBS=4. Use a host that can spare it.
MEM=12g
docker run --rm --memory=$MEM --memory-swap=$MEM --user "$(id -u):$(id -g)" \
    -e HOME=$W/home -e CARGO_HOME=$W/cargo -e JOBS=2 \
    -v $W:$W -w $W/repo terva-crypto-tools:1 \
    terva/complement/crypto/prepare.sh $W/work

# Run, in the host network namespace so the confined ports on 127.0.0.1 are
# reachable, with the daemon's socket.
docker run --rm --network=host --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c %g /var/run/docker.sock)" \
    -v /var/run/docker.sock:/var/run/docker.sock -v $W:$W -w $W/repo \
    -e HOME=$W/home -e RUNTIME=docker \
    -e CRYPTO_DIR=$W/work -e COMPLEMENT_OUT=$W/out \
    -e COMPLEMENT_BASE_IMAGE=localhost/complement-testee:yvx \
    terva-crypto-tools:1 terva/complement/crypto/run.sh
```

Under Docker, `host.docker.internal` resolves to the default bridge's gateway.
`run.sh` reads that address from the daemon and binds the callback server
there alone. It refuses a `TERVA_CALLBACK_BIND` that names anything else. The
address is on the host's `docker0` interface, not on the LAN. A daemon
configured with its own `host-gateway-ip` is not supported.

## With rootless podman

This is the verified path. It ran on 2026-09-24 on the Debian 13 development
host with podman 5.4.2, Go 1.27.1, rustup's 1.95.0 and clang 23.1.1 from mise,
with 16 GiB of RAM. `prepare.sh` took about seven minutes, of which the
matrix-sdk-ffi debug build took 2 min 22 s with `JOBS=4`. The unit it ran in
peaked at 11 GiB. Each suite run took three and a half minutes.

```sh
systemctl --user start podman.socket
mkdir -p ~/.cache/agent-scratch/tuwunel
W=$(mktemp -d ~/.cache/agent-scratch/tuwunel/crypto.XXXX)
terva/complement/crypto/prepare.sh "$W/work"

CRYPTO_DIR="$W/work" \
COMPLEMENT_OUT="$W/out" \
COMPLEMENT_BASE_IMAGE=localhost/complement-testee:latest \
    terva/complement/crypto/run.sh
```

Build the testee image as `terva/complement/rig/README.md` does. Where `clang`
and `jq` come from mise with no version selected, set `MISE_CLANG_VERSION` and
`MISE_JQ_VERSION` for both commands rather than changing mise's configuration.

`RUNTIME=podman` is the default. It uses the rig's `HOST_LOOPBACK=1` service,
whose host gateway reaches the host's loopback, so the callback server stays on
`127.0.0.1`. The rig README describes that mode, and how to clear a rootless
netns that outlived its containers. `run.sh` refuses to start while one exists
without the loopback map.

## Checking a result

```sh
terva/complement/compare.py --baseline terva/complement/baseline-crypto.jsonl \
    "$COMPLEMENT_OUT/results.jsonl"
```

For a run with `RPC=1`, compare against `baseline-crypto-rpc.jsonl`.

On 2026-09-24, against tuwunel 1.9.2-177-g08b045d481:

- **Without the RPC client**, 50 tests passed and 5 skipped. The results were
  byte-identical to upstream's `tests/complement-crypto/results.jsonl`. They
  are `baseline-crypto.jsonl`.
- **With `RPC=1`**, 53 passed and 2 skipped, and a second run gave the same
  results. They are `baseline-crypto-rpc.jsonl`. The three restart subtests
  pass. The two skips are the harness's own:
  - `TestVerificationSAS`: the Rust client cannot be the verifiee yet
    (matrix-rust-sdk#3595).
  - `TestToDeviceMessagesAreProcessedInOrder`: marked flaky.

In every run, each port the confinement recorded was bound to `127.0.0.1`.
