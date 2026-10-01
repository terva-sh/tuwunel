# Focused appservice ping investigation

The focused failures were an **environment/harness startup race on OrbStack**.
A listener bound to `127.0.0.1` can accept local requests before
`host.docker.internal` can forward requests to it. The ping-error helper issued
one-shot test requests without waiting for that callback path. Adding a bounded
setup readiness check made all five focused results pass in three consecutive
runs, without changing ping assertions, response timeouts or the snapshot.

This advances TKT-01M1VZ1VG (Reproduce the separate appservice Complement baseline).
The full appservice suite has not run. Its acceptance criteria remain unchecked.
The correction is confined to the derived tester under `terva/complement/`.

## Loopback-only controls

`terva/complement/probe_callbacks.py` starts a harmless loopback HTTP listener and
prestarted clients. A host-network client connects to `127.0.0.1`; a second
host-network client and a bridge-network client use `host.docker.internal`.
Clients resolve DNS before the listener starts. They request a round-specific
constant body on five fresh dynamic ports. No homeserver, credential, Docker
socket mount or wildcard listener is involved.

Run from the repository root with a new private output directory:

```sh
uv run --no-project python -B terva/complement/probe_callbacks.py \
  "$TERVA_HOME/scratch/callback-control-NEW"
```

The control uses the existing Python image
`sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a`.
Each worker has a read-only root, no capabilities, a 64 MiB memory limit, and
mounts only the script read-only and private output. A unique label identifies
its containers and bridge network. Cleanup verifies labels and immutable IDs.
The script records individual attempts and its source hash in the private report.

The final control, `$TERVA_HOME/scratch/tuwunel-callback-control-03/`, measured:

| Loopback port | Local first success, ms | Tester callback address, ms | Bridge callback address, ms |
| --- | ---: | ---: | ---: |
| 43809 | 3.816 | 345.407 | 362.219 |
| 35425 | 8.380 | 290.153 | 289.794 |
| 41547 | 5.325 | 285.051 | 285.036 |
| 45715 | 5.363 | 297.967 | 301.877 |
| 38795 | 9.834 | 279.722 | 285.134 |

Every local request succeeded on its first attempt. The callback-address clients
initially received connection refusals. Their first successful attempts followed
12–17 attempts, with 20 ms between failed requests. The host-network and bridge
clients observed the same delayed path. The ports are no longer occupied by this
control.

Two earlier controls also reproduced the delay. `tuwunel-callback-control-01/`
compared local and bridge clients. `tuwunel-callback-control-02/` added the tester
callback-address client. All three control namespaces were removed. The final
script hash matches control 03, not the earlier script revisions.

These measurements establish delayed callback reachability on this host, not
OrbStack's internal forwarding algorithm or a guarantee about another runtime.
No daemon configuration, host trust, firewall or forwarding setting changed.

## Focused comparison

The initial loopback tester was
`sha256:21059c7a3de5383c5787f16432914454fadde8948e256d48e6c6062d011a2652`.
It remains available as `terva/complement-loopback:dev`. The corrected tester is
`terva/complement-loopback:callback-ready`:

```text
sha256:5a75c3621629a787d4533597c23da2cc840ac1fa7328095e1962addb2f1dac8e
```

Both use the same original tester parent, pinned Complement revision, testee,
loopback port policy, runner and test selection. The original tester parent is
`sha256:949bd4b237ccf155da89fe929018970fb87471aa8fa2cd71c2d84d8b0e3fe73b`.
The testee remains
`sha256:87f85b98c1434bd3382d48b7b110828b40c58d22448217f585c8809be7b81002`.
Complement is pinned to `b21330e19fc2ee69e043b72cb2072d2f5db03471`.

Private run directories below `$TERVA_HOME/scratch/` record the comparison:

| Directory | Run ID | Focused result |
| --- | --- | --- |
| `tuwunel-ping-original-01` | `16241dad36c2` | Parent and both receiver subtests failed |
| `tuwunel-ping-original-03` | `87888b899ff4` | Same three failures |
| `tuwunel-ping-original-04` | `3a3daa883eae` | Same three failures |
| `tuwunel-ping-ready-01` | `4342168327ba` | All five results passed, 42.583 seconds |
| `tuwunel-ping-ready-02` | `9f075ab22385` | All five results passed, 40.598 seconds |
| `tuwunel-ping-ready-03` | `bf731c02609e` | All five results passed, 40.368 seconds |

The unchanged runs failed `receiver_error_status` and `receiver_timeout` with
connection refusals, returning `M_CONNECTION_FAILED`. Their registration-without-URL
and connection-failure subtests passed. Each corrected run matched all five
corresponding snapshot entries. The readiness probes took 2–3 attempts and
26–48 ms in these focused runs. They did not retry the actual ping operations.
The receiver-timeout assertion now reached its intended timeout instead of
failing immediately to connect.

One additional unchanged run, `tuwunel-ping-original-02`, failed before subtests
because `127.0.0.1:49974` was already in use. It overlapped a harmless control;
the owner of that conflicting socket was not established. Exclude that run from
the callback-readiness comparison. Later unchanged repeats reproduced the actual
receiver failures without overlapping controls. The readiness check is not a
port-collision fix, and a bind conflict still fails closed. Do not run these
controls alongside appservice tests on the same host-network namespace.

## Maintained setup correction

`loopback/apply.go` retains all three original source hash guards. After the
ping-error helper registers its cleanup, it now calls
`tervaWaitForPingCallback(t, port)`. The added helper lives in
`loopback/terva_callback_test.go` and does the following:

1. Read the hostname from the pinned appservice registration URL, or use
   `TERVA_CALLBACK_READINESS_HOST` when it is set. `terva/complement/rig/run.sh`
   sets it to `127.0.0.1`. That rig runs the test process on a Linux host, which
   cannot resolve `host.docker.internal`, so the check there covers only the
   listener.
2. Probe `/_terva_callback_readiness` at that hostname and the bound port.
3. Require HTTP 404, which both mock handlers return without entering their ping
   branches. Send no credentials and do not follow redirects or use a proxy.
4. Retry setup at 20 ms intervals with a 250 ms request timeout and a 10-second
   overall deadline. A failed deadline fails the test rather than continuing.

The new deadline applies only to setup. No original assertion, application
request, expected status, registration or ping response timeout changed.
The mock listeners still bind only to `127.0.0.1`. The original normal bridge
helper already waits for local liveness and `EnsureAppserviceConnection`; the
ping-error helper was the missing readiness check.

`terva_callback_unit_test.go` tests refusal retries, wrong-status retries and
failure at the deadline. Its extra `terva_callback_unit` build tag keeps these
unit tests out of conformance runs. `go test -list` on both images with the
normal appservice tags returned the same 35 top-level tests. Listing tests is not
a full run or proof that every inherited subtest has executed.

Build and repeat the focused test:

```sh
docker build --provenance=false \
  -f terva/complement/Containerfile.loopback \
  -t terva/complement-loopback:callback-ready terva/complement

uv run --no-project python -B terva/complement/loopback.py run \
  "$TERVA_HOME/scratch/complemau-ping-NEW" \
  --tester sha256:5a75c3621629a787d4533597c23da2cc840ac1fa7328095e1962addb2f1dac8e \
  --testee sha256:87f85b98c1434bd3382d48b7b110828b40c58d22448217f585c8809be7b81002 \
  --run '^TestComplemauPingErrorSurfacing$'
```

The build log is `$TERVA_HOME/scratch/tuwunel-ping-ready-build-01.log`.
The Go policy/readiness tests, appservice compilation and all 17 offline Python
runner tests passed. Reapplying the patch failed the source guard without
changing any guarded file.

## Boundary checks and next step

The corrected image also passed `verify_loopback.py`. Evidence is in
`$TERVA_HOME/scratch/tuwunel-ping-ready-validation-01/verification.json`:

- Typing smoke `7cf0f8077489` passed.
- Held fixture `ac027f53ca66` retained two containers, one network, one blueprint
  image and its live endpoint while other runs cleaned up.
- Injected refusal `09a55ec9bb04` returned the expected exit 1, removed its
  container and cleaned up without any real wildcard exposure.
- Repeated cleanup passed. All 24 Complement namespaces across the loopback work
  and this investigation were empty, as were all three harmless control namespaces.

All snapshots, upstream source/build files and original images remain unchanged.
Raw logs and generated credentials remain outside Git. No verified tuwunel defect
was found in these focused failures. The full appservice run and comparison remain
separate work, using the corrected tester and preserving the loopback boundary.

Recorded on 2026-09-06 with Docker 29.4.0, native ARM64 OrbStack on Darwin 25.5.0,
Go 1.26.7 and tuwunel 1.9.0. Terva 0.133.1, commit `ea8f267`, built
2026-09-06T06:08:39Z; extensions index 0.7.0, jmap-mail 0.10.1,
obsidian 0.2.0 and web 0.3.1.
