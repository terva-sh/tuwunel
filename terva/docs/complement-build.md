# Complement image build and readiness

The selected ARM64 images built successfully on 2026-09-06. The testee passed
readiness and trusted TLS probes, but the unmodified harness publishes test ports
on all host interfaces. TKT-01M1W9F9 (Constrain Complement published ports to
loopback) now provides a [derived tester and scoped runner](complement-loopback.md).
Use that runner, not the unmodified harness.

TKT-01M1VZ1VG (Reproduce the separate appservice Complement baseline) remains
unfinished. This phase ran no conformance tests and changed no baseline results.

## Build evidence

The build used the [preflight selection](complement-preflight.md#build-plan)
without `--print`, plus `package_revision` set to the checkout revision:

```text
3069849c64e6f7fff84d0891e499d4c75aab4cf4
```

Source and build-system paths were clean. Only the ticket plan changed during the
build. The existing `orbstack` Docker builder used native `linux/arm64`, four
compile jobs, repository-scoped `fs.read` and `network.host`. It retained the
upstream `test/nightly/all` features and scheduling settings. No Rust source,
upstream Dockerfile or global daemon setting changed.

Bake exited 0 after 761.905 seconds, from 21:01:09 to 21:13:51 UTC.

| Image | Tag | Immutable image ID |
| --- | --- | --- |
| Tester | `complement-tester--debian--testing-slim--aarch64-v1-linux-gnu:latest` | `sha256:949bd4b237ccf155da89fe929018970fb87471aa8fa2cd71c2d84d8b0e3fe73b` |
| Testee | `complement-testee--test--nightly--aarch64-unknown-linux-gnu--all--debian--testing-slim--aarch64-v1-linux-gnu:latest` | `sha256:87f85b98c1434bd3382d48b7b110828b40c58d22448217f585c8809be7b81002` |

Both images remain in the local daemon. Tags can move; use the recorded IDs to
repeat these probes against the same images.

The tester reports Go 1.26.7 on Linux ARM64. The build log and the testee's
federation version endpoint report Rust 1.100.0-nightly, commit `f248f4038`, dated
2026-09-05. Tuwunel reports 1.9.0.

The build log confirms the Complement fetch at
`b21330e19fc2ee69e043b72cb2072d2f5db03471`. The resulting source directory contains
no `.git` metadata, so `git rev-parse` inside the tester cannot verify that ref.
The tester also has no `rustc` executable; obtain compiler evidence from the build
log and running testee rather than assuming the tester is a Rust build image.

Host versions remained Docker 29.4.0, Compose 5.1.2, Buildx 0.33.0 and BuildKit
0.29.0 on Darwin 25.5.0 with a Linux ARM64 OrbStack daemon. The nightly toolchain
and package repositories are moving inputs, so a future rebuild may differ.

## Healthcheck behavior

Neither image defines a Docker `HEALTHCHECK`. The tester exposes no ports; the
testee declares `8008/tcp` and `8448/tcp`.

This absence does not block this pinned Complement version.
`internal/docker/deployer.go:681` implements `waitForContainer` as follows:

1. Wait for Docker health only if `inspect.State.Health` exists.
2. Always require HTTP 200 from `/_matrix/client/versions`.

The testee met that readiness condition in **1.216 seconds**. Docker reported it
running with no health state, not `healthy`. No healthcheck was injected or
silently substituted into the image.

The standalone native health command is a separate diagnostic:

```sh
# Exits 1 because the shared config intentionally lacks server_name.
docker exec "$CID" tuwunel --health-check

# Exits 0 when supplied the same server name as the entrypoint.
docker exec "$CID" tuwunel --health-check '-Oserver_name="hs1"'
```

The first command reported `missing field server_name`. Its failure was a missing
configuration input, not an unhealthy server. The successful second command does
not mean the image has an automatic Docker healthcheck.

## Live endpoint and binding checks

A disposable instance used the original testee image and entrypoint with
`SERVER_NAME=hs1`, `CAP_SYS_NICE`, a two-CPU quota, 2 GiB memory limit and 512-task
limit. It joined a newly labelled bridge network with alias `hs1`. The only host
bindings were explicit dynamic loopback bindings:

```text
127.0.0.1:32769 -> 8008/tcp
127.0.0.1:32770 -> 8448/tcp
```

These ports were assigned to this run and are no longer occupied by it. Actual
Docker `NetworkSettings.Ports` contained only `127.0.0.1`, with no wildcard IPv4
or IPv6 binding.

The probe generated a private CA outside Git and copied it to `/complement/ca`
before starting the container. The unchanged provisioner signed a leaf certificate
for `hs1`. HTTPS requests supplied that CA explicitly and mapped `hs1` to loopback;
certificate verification remained enabled and host trust was untouched.

Eight assertions passed: exact loopback bindings, Complement HTTP readiness,
absence of Docker health state, HTTP and trusted HTTPS client-version endpoints
on both ports, and the trusted HTTPS federation-version endpoint on port 8448.

The probe did not create users, register an appservice or run a Complement test.
It proves startup and endpoint reachability, not appservice conformance or
federation between homeservers.

## Wildcard publishing blocks the suite

The pinned harness sets `PublishAllPorts: true` in
`internal/docker/deployer.go:427`. Its `HSPortBindingIP` chooses an address for
client requests; it does not set the Docker publishing address. The network
creation in `internal/docker/builder.go:496` supplies no loopback binding option.

A control used the actual testee image with `/bin/sleep` replacing the server and
Docker's `--publish-all`. It ran no listener, so no homeserver was exposed. Docker
reported both ports bound to `0.0.0.0` and `::`. This confirms the wildcard
binding on this host without assuming it from source alone. External reachability
through host firewalls or OrbStack forwarding was not tested; wildcard bindings
already violate the selected boundary.

A second control used a newly owned bridge network created with:

```text
--opt com.docker.network.bridge.host_binding_ipv4=127.0.0.1
```

The same `--publish-all` control then bound both ports only to `127.0.0.1`, with no
wildcard IPv6 entry. This identifies a run-scoped mechanism that works on this
host. The completed loopback runner instead supplies explicit bindings in the
common deployer, checks construction and deployment, and fails closed. Its
concurrent cleanup verification passed. This network-default control remains
historical evidence, not a reason to run the suite unmodified.

Do not change the global daemon's bridge defaults or assume that setting
`HSPortBindingIP` makes the current harness safe.

## Cleanup and artifacts

All probe containers and networks were removed using verified labels and immutable
IDs. The two built images and the previously built Linux runner image remain
available. No unrelated container, network or image was removed.

Private artifacts remain under `$TERVA_HOME/scratch/tuwunel-appservice-build-01/`:

- `invocation.json`, `result.json`, `build.log` and `images.json` record the build.
- `complement-docker/` holds the inspected Docker harness sources from the tester.
- `probe-adbbb55edc30/results.json` records readiness, TLS, bindings and native
  health-command results. Its directory retains the private CA, database and logs.
- `tuwunel-complement-bind-8aab6339d767.json` records the per-network binding control.
- `build.py`, `verify.py` and `binding-option.py` preserve the executed probes.

No conformance acceptance criterion was checked. Both the fork-owned and upstream
main/appservice snapshots remain unchanged. The subsequent loopback work passed
a typing smoke. The [focused ping investigation](complement-ping-investigation.md)
then resolved the receiver failures as a callback-startup race. Use the corrected
tester in the [loopback record](complement-loopback.md) for the separate appservice
baseline.

Recorded with terva 0.133.1, commit `ea8f267`, built 2026-09-06T06:08:39Z;
extensions index 0.7.0, jmap-mail 0.10.1, obsidian 0.2.0 and web 0.3.1.
