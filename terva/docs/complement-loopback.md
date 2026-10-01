# Loopback-only Complement runner

TKT-01M1W9F9 (Constrain Complement published ports to loopback) supplies a
run-scoped appservice runner for the native ARM64 OrbStack host. It changes no
Docker daemon setting, host trust, tuwunel source or conformance snapshot.

The derived tester passed the typing smoke, live binding checks, injected refusal
and concurrent cleanup verification on 2026-09-06. The subsequent
[focused ping investigation](complement-ping-investigation.md) identified a
callback-startup race and added a bounded setup readiness check. The corrected
tester passed three focused ping runs and repeated the confinement checks.
TKT-01M1VZ1VG (Reproduce the separate appservice Complement baseline) still owns
the full run and discrepancy classification. This is not a baseline reproduction.

On a host with rootless podman and no Docker, the same confinement runs on
the host through [`rig/prepare.sh` and `rig/run.sh`](../complement/rig/README.md)
instead of the derived tester image. The guard accepts podman's create-time port
allocation and writes its evidence to `TERVA_LOOPBACK_OUTPUT`.

## Reviewed images and source

| Role | Local image ID |
| --- | --- |
| Original tester | `sha256:949bd4b237ccf155da89fe929018970fb87471aa8fa2cd71c2d84d8b0e3fe73b` |
| Unchanged testee | `sha256:87f85b98c1434bd3382d48b7b110828b40c58d22448217f585c8809be7b81002` |
| Initial loopback tester, `terva/complement-loopback:dev` | `sha256:21059c7a3de5383c5787f16432914454fadde8948e256d48e6c6062d011a2652` |
| Current tester, `terva/complement-loopback:callback-ready` | `sha256:5a75c3621629a787d4533597c23da2cc840ac1fa7328095e1962addb2f1dac8e` |

The [original build record](complement-build.md) pins Complement to
`b21330e19fc2ee69e043b72cb2072d2f5db03471`. The tester has Go 1.26.7.
The testee is tuwunel 1.9.0, built with Rust 1.100.0-nightly `f248f4038` dated
2026-09-05. Neither original image has a Docker healthcheck. Complement requires
HTTP `/_matrix/client/versions` readiness instead.

The derived image recipe is `terva/complement/Containerfile.loopback`.
`loopback/apply.go` verifies all three original SHA-256 hashes before writing
any source. An upstream change requires another review, not an updated hash alone.

| Patched Complement source | SHA-256 at `b21330e19` | SHA-256 at `c6d3169d8` (current) |
| --- | --- | --- |
| `internal/docker/deployer.go` | `2e5e19c8323187afa15072ecff28ea9a1a577430d31a6f3ab8103deb99490861` | `17788380b7a690597ea5f8cc493d699bd35eb77b9a006be2d5b3d4275d4c1a93` |
| `tests/complemau/harness_test.go` | `d17023aa70358ffe348d0c2539e393c429d4e21ce3f99bae492e5203941ba5ee` | `9c8abeabd85216d055779a051ab315069fffd1835d17f5a435f4882aacdc9532` |
| `tests/complemau/ping_errors_test.go` | `575722ea3c44f056746f23cfe53e67b85544fc016a8e05490d699d4d4920d5fe` | unchanged |

`apply.go` carries only the current column. The review that moved it is
[below](#re-review-for-pin-c6d3169d8).

The common `deployImage` path now disables `PublishAllPorts` and supplies dynamic
`127.0.0.1` bindings for every exposed TCP or UDP image port. It requires ports
8008 and 8448, checks requested bindings before start, and checks actual
`NetworkSettings.Ports` before accepting the deployment. Missing, extra,
wildcard IPv4 or IPv6, and non-loopback bindings cause refusal. This policy also
rejects `::1`; this runner deliberately uses only IPv4 loopback.

`loopback/terva_loopback.go` implements those checks and writes private structured
evidence for both base-image construction and blueprint deployment. A refusal
removes only the newly created container. The common path also covers dirty
server creation, although the runner disables dirty runs.

The tester uses host networking. Its two mock callback helpers therefore also
bind to `127.0.0.1`, rather than their original `0.0.0.0`. The ping-error helper
also waits for an HTTP 404 on a harmless non-ping path through the callback
hostname. That setup check has a 10-second deadline. No appservice assertion,
registration, ping response timeout or expected response changes. Callback
reachability through `host.docker.internal` depends on the host; do not assume the OrbStack result
applies to a Linux daemon or another desktop runtime.

The original images, upstream files and global Docker configuration remain
unchanged. The derived image is local and trusted code, not a sandbox. Its policy
label is an accidental-misconfiguration guard, not a cryptographic attestation.
Mounting the Docker socket gives it control of the daemon.

## Build and run

Run these commands from the repository root with real Docker and Python 3.11 or
newer. `uv` runs the standard-library-only scripts without a project environment.
Verify the original tester tag resolves to the recorded ID before building:

```sh
docker image inspect --format '{{.Id}}' \
  complement-tester--debian--testing-slim--aarch64-v1-linux-gnu:latest

docker build --provenance=false \
  -f terva/complement/Containerfile.loopback \
  -t terva/complement-loopback:callback-ready terva/complement

docker image inspect --format '{{.Id}}' terva/complement-loopback:callback-ready
```

The recipe accepts `--build-arg TESTER_IMAGE=LOCAL_TAG`. A bare `sha256:...` in
`FROM` did not resolve as a local image on this builder; Docker attempted a
registry pull. Use a verified local tag for the build. The runner resolves its
input references to immutable image IDs before creating resources.

Choose a new private directory below an existing parent outside the checkout:

```sh
TESTER=sha256:5a75c3621629a787d4533597c23da2cc840ac1fa7328095e1962addb2f1dac8e
TESTEE=sha256:87f85b98c1434bd3382d48b7b110828b40c58d22448217f585c8809be7b81002
OUT="$TERVA_HOME/scratch/complemau-smoke-NEW"

uv run --no-project python -B terva/complement/loopback.py run "$OUT" \
  --tester "$TESTER" --testee "$TESTEE"
```

The default selector is `^TestComplemauAppserviceReceivesTyping$`. To select the
full appservice suite, explicitly add `--run '.*'`. That full invocation has not
been run in this work. The runner uses `./tests/complemau/...`, build tags
`conduwuit_blacklist,complemau`, and the upstream skip
`TestComplemauToDeviceBurstFansOut`. It sets `-count=1`, `-parallel=1`, a 30-minute
Go timeout, `GOMAXPROCS=2`, and two CPUs and 2 GiB for tester and testees.

The runner supplies explicit environment values, disables inherited host mounts
and post-test scripts, and generates a random 12-hex run ID after checking for
collisions. It creates a mode-0700 artifact directory and mounts only that output
directory and the Docker socket into the tester. It does not mount the checkout
or host credentials. Ports use Docker's dynamic allocation; the callback helpers
use the pinned suite's port allocation.

A Go failure remains a nonzero runner exit, including an inherited baseline
failure. The runner retains normalized `results.jsonl` for independent comparison.
Success also requires complete test results and verified bindings for both
construction and deployment. For a completed full run, compare individual results:

```sh
uv run --no-project python -B terva/complement/compare.py \
  "$OUT/results.jsonl" --baseline terva/complement/baseline-complemau.jsonl
```

Inspect disappeared, new and newly skipped tests as well as regressions. A partial
smoke is not a full-suite comparison. Do not refresh the snapshot to hide a delta.

## Cleanup and private artifacts

Each run keeps `owner.json`, `invocation.json`, `report.json`, `cleanup.json`,
`bindings.jsonl`, `go.jsonl`, `tester.log` and available container logs. These
files may contain test credentials. Keep the whole directory private and outside
Git. No CA, database, registration or raw log belongs in a commit.

Cleanup stops and removes the outer tester before enumerating its inner resources.
If interruption prevented saving the outer container ID, cleanup looks it up by
its exact run label and verifies its name before stopping it. Ambiguous ownership
fails closed. It checks ownership labels and removes immutable IDs in container,
network, then blueprint-image order. It never prunes or force-removes images.
The original and derived tester/testee images remain available. Cleanup failure
makes the run fail.

SIGINT and SIGTERM request cleanup. After SIGKILL, a host crash or a failed Docker
operation, use the saved owner record to retry:

```sh
uv run --no-project python -B terva/complement/loopback.py cleanup "$OUT"
```

Cleanup is idempotent. Do not edit the owner record or substitute another run's
ID to clear a failure. This ownership mechanism coordinates cooperating runs; it
does not defend against another daemon administrator relabelling resources.

## Verification evidence

Reproduce the binding and cleanup gate with a new directory:

```sh
uv run --no-project python -B -m unittest discover \
  -s terva/complement -p 'test_loopback.py' -v

uv run --no-project python -B terva/complement/verify_loopback.py \
  "$TERVA_HOME/scratch/complemau-verification-NEW" \
  --tester "$TESTER" --testee "$TESTEE"
```

The derived build passed Go policy tests and compiled the appservice package.
All 17 offline Python tests passed. Reapplying the source patch failed its hash
guard without changing any of the three sources.

The initial loopback tester's live evidence is private under
`$TERVA_HOME/scratch/tuwunel-loopback/validation-04/`. The corrected tester's
repeated checks are recorded in the [ping investigation](complement-ping-investigation.md#boundary-checks-and-next-step).
These rows describe the initial image:

| Run | Run ID | Result |
| --- | --- | --- |
| Held construction/deployment fixture | `21f765193186` | Pass, two binding records, 127.950 seconds |
| Appservice typing smoke | `096d98bf1cb2` | Pass, two binding records, 4.877 seconds |
| Injected unsafe inspection | `5f53abd770a9` | Expected exit 1, refused container removed, cleanup passed |

`verification.json` records two held containers, one network and one blueprint
image unchanged while the other runs completed and cleaned up. One construction
container is intentionally stopped; the verifier preserves the original running
states and checks the live deployment's HTTP endpoint after each other cleanup.
It repeats cleanup twice per run. The original images also remain present.

The injected test changes only the inspected binding data to `::`. Actual Docker
bindings stay on loopback, so this failure test never starts a wildcard listener.
A post-run check matched the runner hash and tester ID against all three owner
records. All 14 development and verification namespaces were empty at completion.
`build-05.log` records the initial derived-image build. Earlier attempts remain
private for diagnosis. The ping investigation records the corrected image build.

### Focused ping delta, not a conformance verdict

A separate run selected `^TestComplemauPingErrorSurfacing$` to exercise the second
callback helper. Artifacts are in `tuwunel-loopback/ping-03/`, run ID
`67e1f69e0fe2`. It exited 1 and cleaned up. The registration-without-URL and
connection-failure subtests passed. The receiver-error-status and receiver-timeout
subtests failed with connection refusals through `host.docker.internal`, yielding
`M_CONNECTION_FAILED` instead of their expected errors. The parent also failed.
These three results pass in the inherited snapshot.

The subsequent [loopback-only investigation](complement-ping-investigation.md)
classified the receiver failures as a callback-startup race on OrbStack. The
corrected tester waits for callback readiness during setup and passed all five
focused results three times. No assertion or baseline changed, and no full
appservice suite ran. The initial failed run remains evidence, not a reason to
restore wildcard listeners.

Host versions were Docker 29.4.0, Compose 5.1.2, Buildx 0.33.0 and BuildKit 0.29.0,
Darwin 25.5.0 with a native ARM64 OrbStack daemon. Recorded with terva 0.133.1,
commit `ea8f267`, built 2026-09-06T06:08:39Z; extensions index 0.7.0,
jmap-mail 0.10.1, obsidian 0.2.0 and web 0.3.1.

## Re-review for pin c6d3169d8

Done on 2026-09-23 for TKT-01M37Z3P7FACZQH4TJ8EPT1104 (Re-review the Complement
loopback confinement for pin c6d3169d8), when the upstream 1.9.2 import moved
`complement_ref` from `b21330e19` to `c6d3169d818f4820d52a57b50b3d1453fc814717`.
`prepare.sh` refused the new pin, as it should: two patched files no longer
matched their hashes.

### What changed upstream

- **`deployer.go`** changed by 171 lines in ten commits. 6f2f661 moves the
  Docker client from `github.com/docker/docker` to `github.com/moby/moby/client`
  and `github.com/moby/moby/api`. Calls now take option structs and return
  result structs, `ContainerCreate` takes one `ContainerCreateOptions` with
  `HostConfig` nested inside it, and port types move from `go-connections/nat`
  to `api/types/network`, where `PortBinding.HostIP` is a `netip.Addr`. a6d3306
  namespaces each run's resources, d25aa0d makes the destroy timeout
  configurable, 590af0c adds `COMPLEMENT_TESTEE_CAP_ADD`, and ca5f947 recovers
  the homeserver name from an image tag. Upstream still creates every
  container with `PublishAllPorts: true`, so the confinement is still needed.
- **`harness_test.go`** gained `mustNotReceiveEndpointRequest`, which waits on
  a request queue with a timeout and opens no listener. The patched
  `Hostname: "0.0.0.0"` anchor is unchanged and still unique.
- **`ping_errors_test.go`** is byte-identical.

### What was checked

- **One create path.** Every container is still created in `deployImage`, and
  every caller reaches it: the builder, a deployment and a dirty deployment.
  The patch still covers them all.
- **Same order as before.** Create, check the requested bindings, start, wait
  for ports, check the published bindings, accept. `waitForPorts` ran before
  the URL lookup at the old pin too. The pre-start check already fixes what
  the daemon publishes.
- **Addresses compared as values.** `terva_loopback.go` now compares
  `netip.Addr` with the 4-byte `127.0.0.1`, which fails closed on the zero
  Addr, on `::ffff:127.0.0.1` and on `127.0.0.2`. The unit test covers all
  three, alongside the old wildcard and non-loopback cases.
- **Port strings parsed strictly.** Image-exposed ports are now parsed from
  the image configuration's strings with `network.ParsePort`. A range such as
  `8000-8010/tcp`, junk, or a number above 65535 is refused, not skipped. The
  existing checks still refuse port 0 and any protocol but TCP and UDP.
- **Run ID and label unchanged.** 12 lowercase hex characters, label
  `complement_run_id`, so the run-ID guard and the rejection probe's
  container filter are unchanged.
- **Other listeners unchanged in scope.** Complement's own federation test
  server listened on every interface at the old pin (`:0`) and still does,
  now through a sequential port scan. It was never covered by this
  confinement, because homeserver containers must reach it through the
  gateway. It is recorded here, not changed.
- **Testee configuration.** The rig's testee configuration was regenerated
  from 1.9.2's `docker/Dockerfile.complement`. It is part of the import rather
  than the confinement, but a probe on the old configuration would not test
  the server the suite runs.

### Evidence

Run with rootless podman 5.4.2 on the host, against
`localhost/complement-testee:1.9.2`, whose `/usr/bin/tuwunel` has SHA-256
`6ed315a955a10a4cbe59e83952b24798239c8412047d6f8362522890321b0c57`, the static
1.9.2 build.

- **Build gates.** `prepare.sh` applied the patch, and the three
  `Containerfile.loopback` gates passed on the host:
  - the `TestTervaLoopbackPolicy` unit test;
  - the `TestTervaCallbackReadiness` unit test;
  - a compile of the `complemau` package.

  `go vet` is clean on both patched packages.
- **Live probe, run `d9125c370398`: passed.** The base and blueprint
  containers were both recorded with only `127.0.0.1` bindings. While the
  deployment was held, `ss -ltn` showed its two ports listening only on
  `127.0.0.1`, the client port answered 200 there, and the same port on the
  host's LAN address refused the connection.
- **Rejection probe, run `dc86b2495fc2`: refused as designed.** The injected
  `::` binding was refused with "unsafe binding for 8008/tcp",
  `rejection.json` recorded the refusal, no container with the run's label
  remained, and the test exited nonzero as designed.
- **`run.sh`.** Its integration check accepts the prepared checkout and
  refuses the pristine one.
