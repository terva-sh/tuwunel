# Adopting tuwunel as terva's Matrix backend

Status: active. Ticket epic TKT-01M1QMFW (Adopt tuwunel as terva's Matrix
communications backend).

## Ecosystem scope

The operator confirmed this boundary on 2026-09-06. This repository supplies a
reliable, configurable homeserver for development and as the primary backend for
terva agents. Terva integration is outside the current scope.

The client we will test against is
[terva-conn-matrix](https://github.com/terva-sh/terva-conn-matrix), the existing
Rust Matrix connector in the terva-sh ecosystem. It uses matrix-rust-sdk and
[terva-sdk-rust](https://github.com/terva-sh/terva-sdk-rust), the Rust SDK
exposing protocols and tooling for interacting directly with terva. These are operator-supplied facts, not findings from an
inspection of either sibling repository.

The ecosystem goal is a tested, integrated stack for deploying terva agents.
This repository owns the homeserver configuration contract, server fixes,
development deployment, and repeatable server validation. The connector owns
terva integration and client crypto implementation. Server endpoint checks must
not be reported as a successful connector integration test.

## Why

terva agents need a way to talk to each other. Matrix federates, and that single
property is why it was chosen over a message bus we would have had to build. An
agent can live on its own homeserver and still share a room with agents on other
servers. The deployment boundary becomes a server boundary, which is a boundary
Matrix already knows how to cross, authenticate, and recover from.

Adopting a homeserver rather than only a client library follows from the same
reasoning. If we ship a homeserver alongside terva, a swarm can stand up its own
server and federate outward without depending on somebody else's infrastructure.

`tuwunel` is the successor to conduwuit: Rust, Apache-2.0, a single binary with
an embedded RocksDB store, under active full-time development with the Swiss
government among its deployments. The single-binary property is what makes
shipping it realistic. The active development is what makes forking it a
commitment to keep importing rather than a one-time copy.

Tuwunel is the primary homeserver target for the Matrix connector. Synapse stays
as a second compatibility target. Comparing their behavior helps locate a
failure, but agreement alone does not establish what the specification requires.
Connector integration runs belong to the connector repository.

## Where things stood at the start

The fork was created at `5ff48622a`, identical to upstream `main`, with no local
commits and no divergence. Everything below was decided from that clean base.

## Fork hygiene

`main` mirrors upstream and takes no commits. `terva-main` is ours, is what we
ship, and absorbs upstream by merge rather than rebase, because we publish from
it and rewriting a published branch costs more than a tidy history is worth. Each
merge commit is a dated record of what we imported.

Anything we might eventually offer upstream is authored on a topic branch cut
from `main`, so it carries no terva-only context and cherry-picks into a pull
request cleanly. This is cheap insurance rather than a promise to contribute.

Our own files live only in `terva/` and `.tickets/`, two directories upstream
does not touch, so an import never has to resolve a conflict we created. The
mechanics are in `AGENTS.md`.

## Conformance

Complement is the gate, and `terva/complement/baseline.jsonl` is our snapshot of
it. It is held separately from `tests/complement/results.jsonl` so that an
upstream edit to their file cannot move our gate silently.

The snapshot records 812 distinct tests: 653 pass, 151 fail, 8 skip, across 33
top-level tests that fail. The appservice suite records 97 tests with 4 failing.

`terva/complement/compare.py` diffs a run against the snapshot and reports what
moved. Only a regression fails the exit status. The totals alone are not a
verdict, because one test breaking while another is fixed leaves them unchanged.
That is the whole reason the script exists rather than a line in CI counting
failures.

The main snapshot reproduced byte for byte under real Docker on 2026-09-05,
as recorded in TKT-01M1QMK6B (Reproduce the Complement suite locally and confirm
the baseline). That run completed in about twelve minutes. It establishes the
regression baseline, not the cause of every failure.

The separate appservice snapshot did not run as part of that suite. Reproducing
it is TKT-01M1VZ1VG (Reproduce the separate appservice Complement baseline).
The podman fallback's full run remains unreliable; its environment-dependent
failures are not evidence of server regressions.

`TestSync` and `TestSync/parallel` appear twice each in upstream's own results,
once passing and once failing. They are flaky. The comparison resolves duplicates
by worst result, so they count as failures until they are fixed or quarantined.

The long-term goal is a green suite, ordered by what agent communication depends
on. Three failures sit on the path an encrypted, federated agent conversation
takes: `TestDeviceListUpdates`, `TestDeviceListsUpdateOverFederationOnRoomJoin`,
and `TestFederationKeyUploadQuery`. If an agent on one server cannot see the
devices of an agent on another, an encrypted room shared between them either
fails to establish sessions or silently drops recipients. The silent case is much
worse, because from the sending side the message looks delivered.

Plain federation and baseline reproduction are complete. Both device-list
fixes were done by 2026-09-23:

- TKT-01M1R2N6 (Populate device_lists.left when the syncing user leaves a room)
  was fixed in the fork (PR 10).
- TKT-01M1QMK6C (Fix device list updates and remote key query over federation)
  had two causes. The upstream 1.9.2 import (PR 17) fixed the remote key
  query after a device rename. The fork (PR 20) added sending a joining
  user's device list to servers new to them.

These fixes were required for encrypted-room server readiness, and that
server-side gate is now clear. Readiness itself still waits on real connector
tests; see [homeserver-contract.md](homeserver-contract.md). They do not block
writing the server configuration contract or validating plaintext development
rooms. A green full suite remains the long-term goal; initial readiness must
name its tested behavior and remaining failures.

## Identity

An agent is a virtual user in a namespace owned by an appservice, with devices
managed through MSC4190. `docs/appservices.md` documents the registration format
and `device_management` is already implemented, so the direction is supported
rather than aspirational.

The reason to prefer this over ordinary accounts is that it removes password and
login flows entirely. One token provisions every agent identity, instead of N sets
of credentials to store and rotate. MSC4190 is what extends that from users to
their devices.

TKT-01M1QMK6D (Spike the appservice identity model for swarm agents) completed
17 curl checks demonstrating virtual-user provisioning, device creation, and
bidirectional messaging without a password or login. MSC4190 creates devices;
MSC4326 asserts the device on a masqueraded request.

TKT-01M1QNRGS (Verify agent federation between two homeservers) completed
20 checks from empty databases. Agents exchanged messages across servers, and
cross-server masquerading and use of another server's token were refused.
These were throwaway server proofs, not tests of the actual Matrix connector.
Each server needs its own appservice registration and credentials.

## Shipping and lifecycle

terva does not necessarily own the homeserver process. It may start alongside a
homeserver that is already running and be handed administrative access to it. The
contract is therefore a set of credentials, endpoints, and operator prerequisites,
not a process handle. Classify operations by the interfaces tuwunel actually
supports rather than assuming a generic HTTP admin API.

This is a better fit for federation than supervision would have been. A federated
deployment has servers terva could not supervise even in principle, so a design
that assumed a child process would have needed a second design the moment the
first agent moved to another host.

TKT-01M1QMK6E (Define the homeserver contract for the Matrix connector) records
the [server-side contract](homeserver-contract.md) and
[reproducible requests](../contract/requests.sh). It covers appservice operations,
available administrative interfaces, operator configuration, durable devices,
and restart behavior. Source review and request-construction tests do not replace
live restart or connector validation. Agent routing policy and connector
orchestration are outside its scope.

TKT-01M1VZ1VD (Provide a repeatable development homeserver and recovery recipe)
delivers the [development deployment](development-homeserver.md). The operator
selected Compose with a source-built image, loopback access and private CA trust.
The live drill verified TLS, discovery, persistent signing keys, user/device and
room records, media, and same-image recovery into a new directory. The homeserver
uses only an internal network; a loopback-bound HAProxy relay forwards raw TLS.
This remains a local fixture, not a public federation deployment or connector
integration.

TKT-01M1VZ1VF (Add repeatable homeserver lifecycle and federation checks) adds
[maintained server checks and a fixture contract](lifecycle-checks.md) for later
connector-owned tests. Two fresh runs verified provisioning retries, namespace
rejection, bidirectional messaging, trusted and invalid peer certificates, and
queued delivery after a clean peer restart. A deliberate failure verified
nonzero status and scoped cleanup without interrupting a separately kept fixture.
These are HTTP-level homeserver tests, not Rust connector or E2EE validation.

## Encryption: settled

The first deployment runs unencrypted rooms, and encryption is a per-room
property we will need. Not every room needs it. Some agents will exchange
sensitive information and those rooms must be encrypted; a room coordinating a
build does not.

The constraint that follows is the important half. We start unencrypted, but we
do not get to assume unencrypted. Turning encryption on for a room must not
require rebuilding the design around it.

In practice that forbids four things.

Do not let the room abstraction assume plaintext. Whatever creates a room takes
encryption as a parameter from the first version, even while every caller passes
false. Matrix can enable encryption later, but doing so cannot protect earlier
plaintext history. Our provisioning contract therefore requires the encryption
decision at room creation.

Do not read room content from outside the room. Any feature that works by having
the homeserver, an admin API, or an operator inspect messages will stop working
the moment that room is encrypted. This is the one most likely to be built by
accident, because on an unencrypted room it is easy and it works. Auditing,
logging, and search all have to run inside a room member, not above it.

Do not treat agent devices as disposable. E2EE binds to a device and its keys.
An agent whose device is recreated on restart loses the ability to read anything
sent to its old device. The identity spike already gives every agent a real
device through MSC4190, which is the right shape; provisioning has to keep that
device stable rather than recreating it casually.

Do not send messages through a path that cannot become encrypted. Sending must
go through one abstraction that can grow a crypto layer underneath it, not
scattered direct calls to `/send`.

The existing Matrix connector owns client-side crypto through its
matrix-rust-sdk integration. Its choice of SDK does not establish that encrypted
agent rooms already work. The identity spike tested acceptance of key and
to-device requests, not encryption, decryption, verification, or client recovery.

TKT-01M1QNRGR (Define encrypted-room server requirements for the Matrix
connector) now covers the server capability matrix and validation handoff.
`tests/complement-crypto` and `docker/Dockerfile.matrix-rust-sdk` are reference
material for that work, not a reason to build another client stack here.
Encryption must be ready before sensitive traffic or federation with a server
we do not operate, whichever comes first. Restart-safe client crypto storage
and real encrypted round-trips remain connector-owned implementation and tests.

## Bundling: settled

The operator ruled on 2026-09-20 that the project moves toward a native Matrix
server bundled with terva. terva ships as one static Go binary with an install
script and a build-or-download launcher pattern for companion executables, so the
homeserver ships the same way: a standalone statically linked `tuwunel`
executable built from `terva-main`, run by terva as a native child process. A
container image and the operator-installed package remain possible but are not
the primary shape.

Upstream already publishes standalone `linux-gnu` executables linked with
`crt-static` for `x86_64` at three CPU levels and for `aarch64`, about 33 MB
compressed, so the artifact itself is proven. The work here is to reproduce it
from the fork so that fork fixes ship in the bundle, and to define the
configuration, lifecycle, and evidence a bundled process needs.

This does not move connector work or terva integration into this repository.
The supervision handoff names what terva must do; nothing here implements it.

The work is filed under the adoption epic with the `bundled-server` label:

- TKT-01M2ZMEZD3 (Build a static bundleable tuwunel binary without containers),
  done. `terva/bundle/build.sh` and `bundled-binary.md` are the artifact recipe.
- TKT-01M2ZMEZDY (Define the bundled homeserver configuration profile), done.
  Two modes under `terva/bundle/`: local (plaintext loopback, default) and
  federated (built-in TLS). See `bundled-profile.md`.
- TKT-01M2ZMEZEQ (Run lifecycle and federation checks against the native
  binary), done. `terva/bundle/federation_check.py` runs them as processes.
- TKT-01M2ZMEZFG (Publish fork release artifacts with checksums for terva to
  download), done. Release `terva-v1.9.0-160` is the first.
- TKT-01M2ZMEZGA (Write the bundled homeserver supervision handoff for terva).
  `bundled-supervision.md` is the document terva implements against.

## Open questions

These are open. They are recorded here so that nobody resolves one by being the
first to implement against it.

The distribution format is settled; see "Bundling" below.

Agent addressing, discovery policy, and room-per-task versus long-lived-room
policy belong to connector integration, outside this repository's current scope.
The homeserver contract must expose the supported membership and room operations
without choosing those policies.

Whether we ever contribute upstream. Nothing here depends on it. The topic-branch
convention keeps the option open at no cost, and the decision can wait until we
have a fix worth offering.

## Backlog order to consider

No implementation draft was promoted by this grooming pass. Startable drafts
include the server contract, the located local device-list fix, and the separate
appservice baseline reproduction. The deployment recipe depends on the contract;
the maintained lifecycle checks depend on the deployment recipe.

TKT-01M1VZ1VJ (Refresh host-aware build and conformance guidance) tracks stale
host assumptions in `AGENTS.md` and moves the working Docker recipe out of ticket
notes. The main baseline result above is from the earlier Linux environment,
not a fresh run on the current macOS checkout.

Choose ready work explicitly. Do not treat this suggested order as permission to
promote drafts, select a distribution format, or begin connector integration.
