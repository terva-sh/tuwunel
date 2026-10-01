# Appservice Complement preflight

This records the 2026-09-06 macOS preflight for TKT-01M1VZ1VG (Reproduce the
separate appservice Complement baseline), not a conformance run. The subsequent
[image build and readiness checks](complement-build.md) found wildcard port
publishing. The completed [loopback runner](complement-loopback.md) resolves that
safety blocker and supersedes the wrapper invocation planned below. It passed a
typing smoke. The [focused ping investigation](complement-ping-investigation.md)
then resolved the receiver failures with a bounded callback-readiness check.
The appservice snapshot still contains inherited evidence: 97 results, 93 pass
and 4 fail.

The main baseline was reproduced on the earlier Linux host. See
TKT-01M1QMK6B (Reproduce the Complement suite locally and confirm the baseline).
That run matched upstream byte for byte across 812 distinct tests. Statements in
`AGENTS.md` that it has never been reproduced are stale; the separate guidance
refresh ticket owns that correction.

## Current-host findings

- Darwin 25.5.0, ARM64. Docker context `orbstack` provides a Linux ARM64 daemon
  with 10 CPUs and 8,393,289,728 bytes of memory.
- Docker client/server 29.4.0, Compose 5.1.2, Buildx 0.33.0, BuildKit 0.29.0.
  The existing `orbstack` builder uses the `docker` driver. No new builder or
  host configuration was needed.
- The trimmed Bake plan resolves to 21 targets and exactly one tester and one
  `test/nightly/all` testee, both `linux/arm64`. Every filesystem context is the
  repository root; named contexts reference other targets.
- A two-target probe build executed a dependent target and a `RUN --network=host`
  instruction with only its scratch-directory filesystem grant. This verifies
  those builder capabilities, not compilation of the Complement images.
- Full-graph `bake --check` failed with `unsupported context source target` in
  the Dockerfile frontend. It also exported two empty images under the selected
  tester/testee tags. Both were removed after checking their exact manifest IDs
  and absence of layers. Do not interpret this failure as a Rust build failure,
  or treat those empty images as usable test images.
- macOS `/bin/bash` 3.2 cannot parse `[[ -v ... ]]` in `docker/complement.sh` and
  lacks `${name,,}`. No Homebrew Bash is installed. Do not run the wrapper
  directly with its host shebang.

## Build plan

Run from the repository root. This exact selection passed `--print`. The
[subsequent full build](complement-build.md#build-evidence) also passed, with
`package_revision` set to the checkout revision.

```sh
export docker_dir=docker
export cargo_profiles='["test"]' rust_toolchains='["nightly"]' feat_sets='["all"]'
export rust_targets='["aarch64-unknown-linux-gnu"]'
export sys_targets='["aarch64-v1-linux-gnu"]'
export sys_names='["debian"]' sys_versions='["testing-slim"]'

docker buildx bake --provenance=false \
  --allow="fs.read=$PWD" --allow=network.host \
  -f docker/bake.hcl --set '*.args.nprocs=4' \
  --print complement-tester complement-testee
```

To build, use the same command without `--print`. The plural JSON variables
select one matrix member. The runner later uses singular variables to select its
image names. Do not copy the earlier Linux recipe's `fs.read=/` grant or its
32-job setting. The filesystem grant above covers this checkout only;
`network.host` permits build steps to use the Docker VM's host network.

Expected tags:

```text
complement-tester--debian--testing-slim--aarch64-v1-linux-gnu:latest
complement-testee--test--nightly--aarch64-unknown-linux-gnu--all--debian--testing-slim--aarch64-v1-linux-gnu:latest
```

Keep the upstream scheduling policy and build features for the first attempt.
The development release/default image is not a substitute for this testee.
`nightly`, Debian testing and package repositories are moving inputs. Record the
resolved Rust/Go versions, image IDs and build invocation after building; a plan
alone does not pin them. Native ARM64 differs from the earlier x86-64 run and
must remain part of any result interpretation.

## Linux runner without host installation

`terva/complement/Containerfile.runner` supplies modern Bash and the outer
runner's commands. It does not contain Complement or tuwunel. Build it from the
repository root:

```sh
docker build -f terva/complement/Containerfile.runner \
  -t terva/complement-runner:local terva/complement
```

The identical recipe was built from private scratch during preflight as
`terva/complement-runner-preflight:20260906`:

- Image ID `sha256:3cc58d7ef7f71091e055b68236f1705dd4a8eb8942b5025e5f169b2bd2dee88f`.
- Parent `docker:29.4.0-cli` resolved to
  `sha256:2efe7c8e806b35050def6ba1ba0ddad67b521a0a6d37f4910b3d88f11b495d03`.
- Bash 5.3.3, Docker CLI 29.4.0, zstd 1.5.7; Git, sha1sum and `xargs -r` present.
- Both outer scripts passed `bash -n`. Required Bash features, Docker daemon
  access and Git checks on the baselines passed in the container.

The probe mounted this checkout at `/work` read-only and the Docker socket at
`/var/run/docker.sock`. It used `--network none`, a read-only root filesystem,
`--cap-drop=ALL` and `--security-opt=no-new-privileges`. Git accepted this one
checkout through container-local `GIT_CONFIG_COUNT=1`,
`GIT_CONFIG_KEY_0=safe.directory`, `GIT_CONFIG_VALUE_0=/work`. No global Git
configuration changed. A future run also needs a writable results mount.

Mounting the Docker socket grants control over the daemon even when the mount is
read-only. The probe sent only a version request. Running Complement will create
and remove test containers through that socket. Use only the reviewed runner,
forward explicit non-secret environment variables, and give each run a unique
`RUNNER_NAME` so its cleanup token cannot collide with another run.

The helper's Alpine package versions are not pinned. Rebuilding it requires
recording its new image ID and versions. The tested image remains local for the
next phase; no runner container remains running.

## Network evidence

Four disposable probes passed:

1. A container read Docker `/version` through `/var/run/docker.sock` and saw the
   same Linux ARM64 daemon, API 1.54.
2. The macOS host reached a bridge container's dynamically published loopback port.
3. A host-network container reached that same port through `127.0.0.1`.
4. A bridge container reached a host-network listener bound to `127.0.0.1` through
   `host.docker.internal`. This checks the callback direction used by the tester.

The probes used `python:3.13-alpine`, image ID
`sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a`.
They served only a constant diagnostic response and used no homeserver secrets.
Random run labels and immutable Docker IDs guarded cleanup. Every probe container
and network was removed; unrelated resources were untouched.

Private probe scripts and the JSON report remain under
`$TERVA_HOME/scratch/tuwunel-complement-preflight/`. The network report is
`tuwunel-complement-preflight-3973089f2925.json`. These checks do not establish
Complement healthcheck behavior, TLS behavior or appservice registration.

## Conformance run prerequisites

Use the [loopback runner](complement-loopback.md#build-and-run). Its derived
tester constrains published ports and mock callbacks. The original wrapper and
selectors alone do not constrain Docker's published interfaces.

1. Use the recorded image IDs from the completed build, or repeat its readiness
   and binding checks after rebuilding. Neither image defines a Docker
   healthcheck; this pinned harness accepts that and requires HTTP `/versions`
   readiness instead.
2. Verify the appservice test inventory and run a small smoke test before the full
   suite. Upstream pins `matrix-construct/complement` to
   `b21330e19fc2ee69e043b72cb2072d2f5db03471`.
3. Run `terva/complement/loopback.py` with the recorded derived tester and
   unchanged testee IDs, a new private directory outside Git, and an explicit
   `--run '.*'` only when starting the full appservice suite. The runner supplies
   the pinned appservice build tags and skip selector. It replaces the Linux
   Bash wrapper for this host. Do not invoke the original `docker/complemau.sh`
   or `docker/complement.sh` against the unmodified tester.
4. Compare the full run's individual outcomes, not just totals:

   ```sh
   uv run --no-project python -B terva/complement/compare.py \
     "$OUT/results.jsonl" \
     --baseline terva/complement/baseline-complemau.jsonl
   ```

   Inspect missing, new and newly skipped tests as well as regressions. A zero
   comparison exit status alone does not establish an exact reproduction. Leave
   the snapshots unchanged and classify every discrepancy.

Upstream's Complement testee deliberately allows invalid TLS certificates and
private network peers inside its disposable test configuration. This is not the
hardened lifecycle fixture. Do not copy those settings into development or
production configurations, install its CA on the host, or expose its test servers
publicly. Check actual port bindings before allowing a full run.

No acceptance criterion on the appservice ticket is checked by this preflight.
No conformance suite, connector or E2EE test ran.

Recorded with terva 0.133.1, commit `ea8f267`, built 2026-09-06T06:08:39Z;
extensions index 0.7.0, jmap-mail 0.10.1, obsidian 0.2.0 and web 0.3.1.
