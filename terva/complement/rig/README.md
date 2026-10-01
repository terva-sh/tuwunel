# Running Complement without bake

Upstream drives Complement through `docker/complement.sh`, which is Docker Bake
all the way down. Bake does not run on a host whose `docker` is a podman/buildah
shim, so this directory reconstructs the parts by hand.

**Use `docker/complement.sh` instead if the host has real Docker.** It works,
and it is what produced the conformance answer on TKT-01M1QMK6B: 812 tests,
653 pass, 151 fail, 8 skip, byte-identical to upstream's committed
`tests/complement/results.jsonl`. Build the images with the trimmed bake
invocation on TKT-01M1QSST, the later of the two commands recorded there. The
earlier one builds all twelve testee variants and Complement consumes one. Then
run `docker/complement.sh` bare: it needs no environment, because its own
defaults already match. The suite itself takes about twelve minutes.

This rig is the path for a host without Docker. On the Debian development host
with rootless podman 5.4.2, a full serial run took eight minutes on 2026-09-23
and matched the committed baseline in all 812 results except the one podman
difference described below. An earlier full run on another host, TKT-01M1QSST,
degraded partway through under container load. So a full run here is evidence,
and a result that differs from the baseline is checked against that history
before it is read as a server change.

## What is here

`prepare.sh` clones the pinned Complement fork into a directory outside this
repository and applies the loopback confinement from `terva/complement/loopback`,
the same steps `Containerfile.loopback` runs for the Docker tester. Unpatched,
the harness publishes every homeserver port on all host interfaces, so
`run.sh` refuses a checkout without it.

`Containerfile.testee` builds the homeserver image Complement deploys. It is a
transcription of the `complement-testee` target in `docker/Dockerfile.complement`,
with `complement.toml` and `v3.ext` copied verbatim from that file. The base is
`debian:trixie-slim` by default, which suits a static binary: a release asset
or the output of `terva/bundle/build.sh`. For a dynamic binary from
`terva/build.sh`, pass `--build-arg BASE=localhost/terva/tuwunel-build:latest`.
Two deliberate differences from upstream are commented in place: the
HEALTHCHECK, which upstream injects from `docker/bake.hcl` rather than the
Dockerfile, and its command, which upstream's `tuwunel --health-check` cannot
satisfy outside upstream's build.

`run.sh` runs `go test` on the host against podman's Docker-compatible API,
skipping upstream's tester container. That container exists to give CI a
hermetic toolchain and buys nothing locally.

## Running it with rootless podman

Verified on 2026-09-23 with rootless podman 5.4.2 on Debian 13, Go 1.27 on the
host, and no Docker installed. Run from the repository root.

1. Start the API socket that `run.sh` talks to:

   ```sh
   systemctl --user start podman.socket
   ```

2. Prepare the harness in a new directory outside the repository. Put it in a
   fresh directory under the host's swept scratch root, `~/.cache/agent-scratch/`,
   rather than anywhere else in `~/.cache`:

   ```sh
   mkdir -p ~/.cache/agent-scratch/tuwunel
   W=$(mktemp -d ~/.cache/agent-scratch/tuwunel/complement.XXXX)
   terva/complement/rig/prepare.sh "$W/complement"
   ```

3. Assemble the testee build context outside the repository, so the working
   tree stays clean, and put a static `tuwunel` in it:

   ```sh
   ctx="$W/testee"
   mkdir -p "$ctx"
   cp terva/complement/rig/{Containerfile.testee,complement.toml,v3.ext} "$ctx"/
   cp docker/lib/{provision_complement.sh,sched_wrap.sh,perf_wrap.sh} "$ctx"/
   cp PATH/TO/STATIC/tuwunel "$ctx"/tuwunel
   ```

4. Build the image. `--format docker` is required: podman defaults to OCI,
   which has no HEALTHCHECK field and drops it silently, and Complement then
   fails every test at deploy.

   ```sh
   podman build --format docker -t localhost/complement-testee:latest \
       -f "$ctx"/Containerfile.testee "$ctx"
   ```

5. Run a focused test:

   ```sh
   COMPLEMENT_SRC="$W/complement" \
   COMPLEMENT_OUT="$W/out" \
   COMPLEMENT_BASE_IMAGE=localhost/complement-testee:latest \
   RUN='^TestDeviceListUpdates$' \
       terva/complement/rig/run.sh
   ```

`RUN` limits the test regex and `TAGS`, `TESTS`, `SKIP`, `PARALLEL` and
`TIMEOUT` override the rest. The defaults match `docker/complement.sh`: tag
`conduwuit_blacklist` alone, `./tests/...`, its skip list, serial, no shuffle.

### The appservice suite

`docker/complemau.sh` runs a separate suite with its own baseline,
`terva/complement/baseline-complemau.jsonl`. Pass it what that script passes,
and `HOST_LOOPBACK=1`:

```sh
HOST_LOOPBACK=1 \
TAGS=conduwuit_blacklist,complemau \
TESTS=./tests/complemau/... \
SKIP=TestComplemauToDeviceBurstFansOut \
COMPLEMENT_SRC=... COMPLEMENT_OUT=... COMPLEMENT_BASE_IMAGE=... \
    terva/complement/rig/run.sh
```

The suite's homeserver calls back to appservice listeners in the test process,
which the confinement binds to `127.0.0.1`. Rootless podman's host gateway,
169.254.1.2, reaches the host's LAN address instead, so without
`HOST_LOOPBACK` every callback is refused and nearly every test fails. With it,
`run.sh` starts a podman service of its own, reading `containers-hostloop.conf`,
whose pasta maps 169.254.1.3 to the host's loopback and points
`host.docker.internal` there. Containers on that network can then reach every
service on the host's loopback for as long as the run lasts, not only the
harness.

Pasta takes the option only when it creates the rootless netns, which every
bridge network of the user shares, so `run.sh` refuses when one already exists
without it. Wait for the user's other podman containers to finish. While a run
holds the netns, any other bridge container the user starts shares the loopback
map.

A netns whose ref-count outlived its containers is never torn down. Remove a
leaked one by hand only when `podman ps -a` is empty. The steps are all under
`$XDG_RUNTIME_DIR/containers/networks/`:

1. Stop its pasta process, named in `rootless-netns/rootless-netns-conn.pid`.
2. Stop its aardvark-dns, named in `aardvark-dns/aardvark.pid`. It lives inside
   the old netns. Left running, netavark reuses it, and container names stop
   resolving, so every federation test hangs at `make_join`.
3. Unmount `rootless-netns/rootless-netns` inside `podman unshare` and delete
   that directory.
4. Delete any file under `aardvark-dns/` whose network `podman network ls` no
   longer lists. A fresh aardvark-dns loads every file there, and one sharing a
   gateway with a new network makes it fail with "Address already in use".

The suite's callback-readiness helper dials the registration host from the test
process. That resolves inside upstream's tester container but not on a Linux
host, so `run.sh` sets `TERVA_CALLBACK_READINESS_HOST=127.0.0.1` and the check
covers the listener alone.

`run.sh` pipes through `jq`. Where jq comes from mise without a selected
version, set `MISE_JQ_VERSION` for the run rather than changing mise's
configuration.

The confinement writes one line per container binding to
`$COMPLEMENT_OUT/loopback/bindings.jsonl`. Every `HostIp` there must be
`127.0.0.1`; the guard refuses anything else before a test runs.

### Where podman differs from Docker

Podman allocates a requested dynamic host port when the container is created,
where Docker leaves it empty until start. The confinement accepts either and
still refuses any address but `127.0.0.1`.

`TestDeviceListsUpdateOverFederation/interrupted_connectivity` fails under
podman and passes in the Docker baseline. The test stops hs2, has hs1 queue a
device-list update, and starts hs2 again. Under podman a stopped container's
name stops resolving at once and it returns on a new address, so hs1's send
fails immediately. The server then backs off for at least `sender_timeout`,
300 s in `complement.toml`, which outlasts the test's 50 s wait. The server is
doing what its configuration says; the difference is the runtime's. See
TKT-01M37KN3 for the probe.

## Checking a result

```sh
terva/complement/compare.py "$COMPLEMENT_OUT/results.jsonl"
```

Note that upstream gates differently: `docker/lib/complement-runner.sh`
overwrites `tests/complement/results.jsonl` and runs `git diff --exit-code`, so
any change fails, including an improvement. `compare.py` fails only on a
regression and names what moved.
