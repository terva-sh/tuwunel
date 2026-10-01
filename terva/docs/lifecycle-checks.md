# Repeatable homeserver lifecycle and federation checks

`terva/lifecycle/run.py` checks two real tuwunel homeservers using Matrix HTTP
requests. It covers TKT-01M1VZ1VF (Add repeatable homeserver lifecycle and
federation checks). It does not run `terva-conn-matrix`, matrix-rust-sdk or terva.
Passing this command is server evidence, not connector integration or E2EE proof.

A native form of these checks exists for the bundled binary:
`terva/bundle/federation_check.py` runs three servers as plain processes under
test authorities and makes the same assertions plus certificate reload. See
[bundled-profile.md](bundled-profile.md). The Compose fixture here remains the
one that writes `fixture.json` for connector-owned tests.

## Run from fresh state

Use the [development deployment prerequisites](development-homeserver.md): local
Docker 29 or newer, Compose, OpenSSL 3, Git, uv with Python 3.11 or newer, and a
non-root operator. Run from this repository's root. Build the native homeserver
image through the existing wrapper, then supply the tag it prints:

```sh
terva/development/build.sh
docker pull haproxy:3.2-alpine
IMAGE="terva/tuwunel-dev:REPLACE_WITH_SOURCE_COMMIT-arm64"
BASE="$HOME/tuwunel-lifecycle"
mkdir -p -m 700 "$BASE"

uv run --no-project python -B terva/lifecycle/run.py run "$BASE/run-001" \
  --image "$IMAGE" --relay-image haproxy:3.2-alpine
uv run --no-project python -B terva/lifecycle/run.py run "$BASE/run-002" \
  --image "$IMAGE" --relay-image haproxy:3.2-alpine
```

Use `-amd64` when that is the tag the build prints. The directory must not exist
and its parent must already exist outside every Git worktree. The runner creates
private files there, generates new server names and credentials for each run,
selects unused loopback ports, and pins both runtime images by local image ID.
It never pulls or builds images implicitly. A concurrent port allocation can
still race another process; a bind failure fails the run rather than changing
an unrelated service. Retry with another new directory after checking the cause.

A successful run exits 0. An assertion, timeout, setup failure or cleanup failure
exits nonzero. `results.json` records named assertions, server/event identities,
elapsed delivery times, tool versions, image IDs, checkout commit/dirty state,
and hashes of the runner and reused development helper. `fixture.json` describes
the endpoints and credential-file inputs. `owner.json` records resource names.
The server logs remain private files for diagnosis, not terminal output.

## What the assertions prove

- Each appservice registers one local virtual user without receiving a login
  token. Repeating registration returns `M_USER_IN_USE`; it is not a second
  successful registration. Repeating device PUT changes HTTP 201 to 200 while
  retaining exactly one device with the same ID.
- A valid local token cannot masquerade as a local user outside its namespace
  or as the other server's user. A token from the other server is rejected.
- A local plaintext event appears in the sender's sync timeline. After a remote
  invite and join, both servers receive events sent by the other. Repeating a
  send with the same transaction and device retains its event ID.
- Before federation succeeds, peer B serves a leaf certificate signed by its
  trusted CA but missing B's hostname. B's localhost TLS endpoint remains valid.
  A remote-profile request made by tuwunel A fails. OpenSSL independently confirms
  a hostname mismatch. The runner stops B, selects its correct certificate, and
  requires the same remote-profile request on A to succeed. This is a negative
  control on tuwunel's outbound federation, not just a Python client trust test.
  Release builds omit the TLS debug log, so the test does not depend on that log.
- The runner captures B's sync cursor and stops B with exit code 0. It sends an
  event on A while B is stopped, checks transaction retry semantics, and records
  local acceptance as `delayed`, not delivered. After five seconds it restarts B
  on its original data. Only B's sync timeline can confirm the event as delivered.
  The runner does not resend that event after B restarts.
- B retains its signing keys, user, device ID, device display name and room
  membership across restart. Both servers report both room members. A new event
  sent by B reaches A after recovery.

The delivery test never calls a remote event-fetch endpoint that could retrieve
an otherwise undelivered event on demand. It advances a sync cursor only after a
successful response and fails if the small fixture's timeline is limited.

`--deadline 120` is the default per delivery/profile-recovery deadline. Values
10 through 300 are accepted. Transient connection errors and read timeouts can
retry within that deadline without discarding the last confirmed cursor. Other
HTTP assertions fail immediately. One in-flight HTTP request can add up to five
seconds to the deadline. Startup readiness has its separate 120-second bound;
clean peer shutdown allows 60 seconds. These are not a single whole-run timeout.
An unresponsive Docker daemon is an operator failure, not a peer-outage result.

A delivery deadline means "not observed within the test window". It does not
prove permanent message loss. The test reduces federation connection/request and
sender retry intervals in its explicit test profile; it does not claim to measure
production backoff timing. The server's queued sender, not a client retry after
recovery, must eventually provide the delayed event.

## Network and certificate profile

The runner reuses `terva/development/dev.py`, its Compose template and raw-TLS
HAProxy relay. It changes only newly generated lifecycle instances, never the
single-server recipe's defaults or an existing operator instance.

Each homeserver remains on internal networks with no published host port. A
shared, labelled internal Docker network supplies fixed peer IPs and `.test`
DNS aliases. Each relay publishes only its own loopback TLS port. The runner
checks actual network attachments, internal-network flags, peer IPs, loopback
bindings and the CA environment on startup and restart.

`SSL_CERT_FILE=/tls/peers.pem` supplies a bundle of the two instance CAs to each
homeserver. `allow_invalid_tls_certificates` remains false. The generated
`ip_range_denylist` covers the entire IPv4 and IPv6 address space except the two
exact peer IPv4 addresses. This is narrower than clearing the private-IP denylist
or allowing a whole Docker subnet. Offline tests check the address-space coverage.
Do not copy the peer exceptions into a deployment profile.

No command installs host CA trust, edits host DNS, exposes public federation,
uses a host network, or grants extra Linux capabilities. Each homeserver retains
the development limits of 2 CPUs, 2 GiB and 512 tasks; each relay has 0.25 CPU,
64 MiB and 32 tasks. The four containers consume less than those limits in the
small fixture, but the limits are not capacity estimates.

## Keep a fixture for connector-owned tests

```sh
uv run --no-project python -B terva/lifecycle/run.py run "$BASE/connector-001" \
  --image "$IMAGE" --relay-image haproxy:3.2-alpine --keep
# After the connector owner's tests finish:
uv run --no-project python -B terva/lifecycle/run.py cleanup "$BASE/connector-001"
```

Only a successful `--keep` run leaves containers running. Consume `fixture.json`
only after `results.json` reports `status: passed`. The contract format is 1:

| Field | Meaning |
|---|---|
| `run_id` | Unique run owner, also used in Docker labels |
| `connector_integration_validated` | Always false for this server-only runner |
| `delivery`, `room_encrypted` | Appservice polling; the seeded room is plaintext |
| `peers_network` | Internal network for explicitly attached test clients |
| `nodes[].endpoint` | HTTPS localhost URL for a host-side connector |
| `nodes[].container_endpoint` | HTTPS `.test:8448` URL on the peer network |
| `nodes[].server_name` | Immutable Matrix identity; not the host-side endpoint |
| `nodes[].ca_file` | Public CA to trust explicitly for that node |
| `nodes[].credentials_file` | Private JSON containing `as_token` and `hs_token` |
| `nodes[].appservice_id`, `sender`, `namespace` | Registration ID, sender user and anchored local namespace |
| `nodes[].user_id`, `device_id`, `room_id` | Existing probe identities and joined room for readback |
| `nodes[].directory`, `image` | Private instance location and pinned homeserver image |

The connector owner must read `as_token` through their secret input, configure
explicit CA trust, and persist their own user/device mapping and crypto stores.
Do not put tokens on command lines or mount a whole instance into a client.
Mount only the public CA and the required private credential file, read-only,
with permissions appropriate to the test client's UID. Callback delivery is not
configured. The probe device belongs to this test; use separate virtual users
and stable devices for actual connector tests, within the supplied namespace.

The server fixture is only an input to tests owned by `terva-conn-matrix`.
This ticket neither inspected nor changed that repository or `terva-sdk-rust`.
Real encrypted round-trips, client store recovery, routing and agent integration
remain connector-owned validation. See the [homeserver contract](homeserver-contract.md).

## Failure and cleanup

Exercise the failure path deliberately with a new directory:

```sh
uv run --no-project python -B terva/lifecycle/run.py run "$BASE/failure-001" \
  --image "$IMAGE" --relay-image haproxy:3.2-alpine --keep --fail-after-setup
```

This must exit 1 and report `status: failed`, a false assertion and
`cleanup: passed`. `--keep` does not retain a failed fixture.

Cleanup enumerates only resources carrying the random run label, validates their
names and owner labels, and uses immutable Docker IDs to stop/remove them. It
never calls prune, removes an image, deletes a database directory, or takes down
another Compose project. Repeating the cleanup command is safe. The command's
output describes that later cleanup; `results.json` preserves the outcome of the
original run, including whether it was deliberately kept then.

A cleanup error makes the run fail and remains in `results.json`. SIGTERM and
Ctrl-C enter the cleanup path, but SIGKILL, host loss and daemon failures cannot
guarantee cleanup. Re-run the scoped cleanup command after restoring Docker.
Retain private logs for diagnosis and review paths before deleting any artifacts.
The directories contain live credentials, TLS keys and database state even after
containers are gone. They are not encrypted backups and must remain outside Git.
The development fixture's 30-day leaf-certificate limit still applies.

## Offline tests and limits

```sh
uv run --no-project python -B -m unittest discover -s terva/lifecycle -v
```

The offline tests cover exact peer-IP exceptions, deadlines, cursor retention,
limited timelines, failed assertions, private reports and cleanup ownership.
The live test covers one room, two servers and a clean temporary peer stop. It
is not a packet-loss/partition test, a crash-corruption test, a performance test,
a full namespace attack corpus or Complement. IPv6 federation, public DNS
federation, AMD64 and Linux-host ownership need separate runs. The main and
appservice Complement baselines remain independent gates.

## Recorded validation on 2026-09-06

Two fresh ARM64 runs of the final runner each passed 32 assertions:

| Run directory under the operator's scratch directory | Result | Outage-event sync observation after readiness |
|---|---|---|
| `tuwunel-life-validation-01` | Passed, explicitly kept | 30.258 seconds |
| `tuwunel-life-validation-02` | Passed, automatically cleaned up | 26.242 seconds |
| `tuwunel-life-validation-failure` | Expected exit 1, false assertion, cleanup passed despite `--keep` | Not exercised |

The first run's four containers and five networks remained intact and its two
validated TLS endpoints remained reachable after the other runs cleaned up.
The explicit cleanup command then removed that fixture; repeating cleanup also
passed. All private data and reports remained outside Git. These observations
exercise both the kept fixture contract and cross-run cleanup isolation.

The test reused the already built tuwunel 1.9.0 ARM64 release/default-feature
image with direct TLS, Rust 1.95.0 and source commit
`5d4866e39778492e265e6ae412943c11f9efa982`:

- Homeserver `sha256:791a229131280cdbe4fa28fc285a5a4e7eda152ee7ce9cb54f552f176b821950`.
- HAProxy `sha256:6343ce34a132a5dceaa24767d739df2bd519f8f7c1079ae39e4821334e8eb42e`.

Host tools were Docker 29.4.0, Compose 5.1.2, OpenSSL 3.6.1 and Python 3.13.2 on
macOS. No Rust source changed in this ticket. The first development run relied
on a TLS debug log that the release image omits. Another run encountered a
transient recovery sync timeout. The final runner uses the certificate negative
control and bounded cursor-preserving retries instead. Those early assertion failures also cleaned up their resources.

The recording agent ran terva 0.133.1, commit `ea8f267`, built
`2026-09-06T06:08:39Z`, with index 0.7.0, jmap-mail 0.10.1, obsidian 0.2.0 and web
0.3.1. No connector, SDK, mail, web or sibling-repository work participated.
