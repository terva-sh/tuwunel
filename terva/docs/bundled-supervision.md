# Supervising the bundled homeserver: a handoff for terva

Status: handoff written for TKT-01M2ZMEZGA (Write the bundled homeserver
supervision handoff for terva) on 2026-09-23, against release
`terva-v1.9.0-160`.

This document is for whoever builds the supervisor in terva that downloads,
starts, watches, stops and upgrades a bundled `tuwunel`. It says what that
supervisor must do and why, in the order the supervisor meets each concern,
and links the documents that hold the detail. Nothing here is implemented in
terva. Passing every check named below proves the server, not terva
integration, and not encrypted-room readiness. The server-side device-list
fixes that gated it, TKT-01M1QMK6C (Fix device list updates and remote key
query over federation) and TKT-01M1R2N6 (Populate device_lists.left when the
syncing user leaves a room), are done. Readiness still waits on the connector
tests described in [homeserver-contract.md](homeserver-contract.md).

`terva/bundle/bundle.py` is the reference implementation of everything below.
It is Python and terva is a static binary, so terva may reimplement it rather
than ship it. Either way, the contract is the state directory and the files in
it, not the script. A reimplementation that writes the same layout gets the
same server behaviour.

Each rule carries its evidence in the last section: a check that ran, a
document that recorded one, or source code that was read and not exercised.

## Get the artifact and prove it is the right one

A fork release is a tag `terva-v<upstream version>-<fork commits>` on
`terva-main`. Each release publishes a zstd-compressed static binary under
upstream's naming and a `SHA256SUMS` file. The full recipe is in
[bundled-binary.md](bundled-binary.md#releases).

1. Pick the asset by architecture and CPU level. Only `x86_64-v1` exists.
2. Download the asset and `SHA256SUMS`.
3. Verify the checksum before you decompress.
4. Decompress, then run `tuwunel --version`.
5. If the version string does not start with the version the tag names,
   treat the download as corrupt.

Nothing is signed. The checksum proves the download matches what the release
job published, and nothing more.

## Give the server one state directory and own it

`bundle.py init` creates the directory. The supervisor owns it from then on.
The layout and what each file holds are in
[bundled-profile.md](bundled-profile.md#state-directory). The parts a
supervisor acts on:

| Path | What the supervisor does with it |
| --- | --- |
| `bundle.json` | Reads the mode, server name, port, binary path and binary digest. Rewrites the binary fields on upgrade. |
| `config/` | Passes the three files to the server with `-c`. Reads the tokens from `appservice.toml`. |
| `data/` | Never touches it while the server runs. Backs it up together with `config/` and `bundle.json`. |
| `run/` | Free for the supervisor's pid file and logs. |
| `tls/` | Federated mode only. Replaces `server.crt` and `server.key` on renewal. |

Every file is mode 0600 and every directory 0700. The directory must sit
outside any Git worktree, which `init` enforces, because it holds the tokens
and the server's signing keys.

Back up `bundle.json`, `data/` and `config/` together, and restore them
together. The database holds the signing keys and the device records that the
registration in `config/` refers to, and `bundle.json` names the binary that
last opened the database. A partial restore leaves them disagreeing.

## First run: choose the name once

```sh
terva/bundle/bundle.py init ROOT --binary PATH --server-name NAME [--port N]
```

`local` is the default mode: plaintext on loopback, federation off, no
certificates. [bundled-profile.md](bundled-profile.md#two-modes) describes
`federated` mode and its flags.

The server name has no default because it cannot change after the database is
created. Every user ID, room and signing key is bound to it. A deployment
named `localhost` can never federate. If there is any chance a deployment will
federate later, choose a name it could federate under now. Changing the mode
later means editing two files and restarting. Changing the name means a new
database and new agents.

The host must have a CA bundle, even in local mode. Since 1.9.0 the binary
refuses to start without one. A host with `ca-certificates` installed passes.
On a minimal host, set `SSL_CERT_FILE` in the child's environment. Do not
point `SSL_CERT_FILE` at a private CA to trust a peer, because it replaces the
platform roots and every public HTTPS destination then fails. Pass the
private root to `init --trust` instead, which writes
`[global.tls] trusted_roots`.

`init` prints the command line. `bundle.py command ROOT` prints it again
later:

```text
tuwunel -c ROOT/config/profile.toml -c ROOT/config/tuwunel.toml -c ROOT/config/appservice.toml
```

The order matters. Later files override earlier ones.

## The registration and its two tokens

`init` writes one appservice registration, ID `terva`, into
`config/appservice.toml`. It generates both tokens itself from 48 random bytes
each. terva does not choose them. It reads them from that file.

| Field | Default | Use |
| --- | --- | --- |
| `as_token` | generated | terva's connector sends it as a bearer token, asserting an agent with `user_id` and `device_id`. One token covers every agent. |
| `hs_token` | generated | The server sends it to a callback URL. The bundled registration has no URL, so the server never uses it. |
| `sender_localpart` | `terva_service` | The privileged appservice user. It is not an agent. |
| Namespace | `^@terva_[a-z0-9_]+:NAME$` | Exclusive. Change the prefix with `init --namespace-prefix`. |
| `device_management` | `true` | Agents get durable MSC4190 devices without logging in. |

No URL means agents receive events by polling `/sync` as their own device.
Callback delivery is possible but untested in the bundled shape. It needs a
`url` in the registration and a listener in terva that authenticates
`hs_token`. [homeserver-contract.md](homeserver-contract.md#room-encryption-and-delivery)
describes what callbacks change.

The server reads the registration only at startup. `SIGUSR1` does not reload
it. To rotate the tokens, stop the connector, write new tokens into
`appservice.toml`, restart the server, and confirm the old `as_token` is
refused. There is no period during which both tokens work. The contract's
[rotation section](homeserver-contract.md#retirement-and-credential-rotation)
has the full sequence.

Keep the tokens out of command lines, environment dumps and logs. Never run
the server's `show-config` admin command into a log, because it prints them.

## Run, watch and stop the process

| Concern | Rule |
| --- | --- |
| Start | Run the command line with the state directory's files. One process per `data/`. RocksDB locks the database and a second process fails to open it. |
| Health | Run the same command line with `--health-check` appended. Exit 0 means the running server answered. Poll every second after start. On a fresh database it passes within about two seconds. `bundle.py` gives up after 60 s. |
| Logs | Everything goes to stderr, one line per event, no colour codes. The supervisor captures and rotates it. |
| Stop | Send `SIGTERM`. The server drains client requests for up to 15 s and federation sends for up to 5 s, then exits 0. Wait 60 s before `SIGKILL`. |
| Reload | `SIGUSR1` rereads the reloadable settings and the TLS certificate and key. A failed certificate reload logs and keeps the old pair. |
| Forbidden | Do not send `SIGHUP`. The server does not handle it, so it kills the process like an unclean stop. |
| Exit | Any exit the supervisor did not ask for is a failure. Read the last stderr lines before restarting. When the server refuses its database at startup, it exits the same way, and a restart will not help. |

Startup logs one of these lines. Watch for them:

| Line | Meaning |
| --- | --- |
| `Created new RocksDB database with version N` (WARN) | First run on an empty `data/`. On any later start, `data/` was lost. |
| `Loaded RocksDB database with schema version N` (INFO) | Normal start on an existing database. |
| `Database: migrated schema version from X to Y.` (INFO) | An upgrade changed the schema. The old binary can no longer open it. |
| `Database schema version X is newer than this build supports` | Startup refused. See the downgrade rules below. |

## What survives a restart

A restart with the same command line and the same directory keeps every
agent's account, device, room membership and messages, and the server's
signing keys. Messages accepted for a federated peer that was down are
delivered from the queue after it returns, without a resend.

The server does not keep what belongs to the connector. terva must persist
these, because a server backup cannot restore them:

- the mapping from each logical agent to its Matrix user ID and device ID,
- room IDs and pending transaction IDs,
- each device's sync cursor, and
- each device's crypto store.

On restart, the connector reads its expected device first. An unexpected 404
means server state was lost or changed. Recreating the device hides that, and
a recreated device ID does not recover lost keys.
[homeserver-contract.md](homeserver-contract.md#provisioning-retries-and-durable-state)
has the full ownership table.

## Upgrade forward, and go back only through the backup

`bundle.py command` and `check` refuse a binary whose SHA-256 differs from the
one in `bundle.json`. An upgrade is therefore a deliberate step. `bundle.py`
has no upgrade command, so the supervisor records the new binary itself.

1. Download and verify the new release as described above. Install it at a
   path that names its version, and keep the previous binary where it is.
2. Stop the server with `SIGTERM` and wait for exit 0.
3. Copy `bundle.json`, `data/` and `config/` together, while the server is
   stopped. `bundle.json` names the old binary, and a rollback needs it.
4. In `bundle.json`, set `binary`, `binary_sha256` and `version` for the new
   binary.
5. Start the server and read the startup line.
6. Wait for the health check to pass before starting the connector.

To roll back, stop the server and restore all three from step 3. The restored
`bundle.json` selects the old binary again, which must still be at its
recorded path with the recorded digest. If you restore `data/` alone, the next
start runs the new binary and migrates the database forward again.

The database carries a schema version. tuwunel 1.9.0 is at version 17. The
server checks it at startup:

- A newer binary migrates an older schema forward. After that, the older
  binary refuses the database.
- An older binary refuses a newer schema and exits.
- An older binary with the same schema version opens the database.

The last rule is only the startup check. It does not prove the older binary
reads every record the newer one wrote, because a release can change stored
data without changing the schema version. The backup from step 3 is the
recovery path for every upgrade. Swapping the old binary back without
restoring was tested for one pair, 1.9.0-110 and 1.9.0-160, and is not a rule
for other pairs.

The server setting `force_migration` makes an older binary open a newer
database anyway. It stamps the schema down and can lose data the newer binary
wrote. Never set it in a bundled deployment.

## Who owns what

The server guarantees, for this release, what the checks in the last section
prove: appservice provisioning, namespace isolation, durable devices, restart
persistence, per-room encryption choice, queued federation delivery, and
certificate reload.

The connector, [terva-conn-matrix](https://github.com/terva-sh/terva-conn-matrix), owns and must validate:

- identity and device persistence against a real server,
- its chosen delivery mode, polling or callbacks,
- mixed plaintext and encrypted rooms, and reading back room encryption state
  before sending,
- namespace refusal and token rotation from its side, and
- encryption, key sharing and verification, none of which the server can do
  for it.

The operator decides, and nothing in this repository decides for them:

- whether a deployment federates at all, and with whom,
- the server name, DNS and delegation, and the `.well-known` documents,
- certificates for federated mode, their authority and renewal,
- firewall exposure of a non-loopback listener,
- backup schedule and retention, and
- resource limits for the host.

## Evidence for each rule

| Rule | Evidence |
| --- | --- |
| Release asset, checksum and version string | Checked. TKT-01M2ZMEZFG (Publish fork release artifacts with checksums for terva to download), and again on 2026-09-23 for this handoff: `SHA256SUMS` OK, version `1.9.0-160 (v1.9.0-160-g194987ad3d)`. |
| State directory layout, modes, permissions | Checked. `bundle.py check`, 15 of 15 in each mode, [bundled-profile.md](bundled-profile.md#recorded-validation-on-2026-09-20). |
| CA bundle required at startup | Checked by reproduction. [bundled-profile.md](bundled-profile.md#first-run). |
| Registration fields and generated tokens | Read from `bundle.py`, `appservice_text` and `init`. Exercised by every `check` run. |
| Registration read only at startup | Read from source, [homeserver-contract.md](homeserver-contract.md#operator-prerequisites). Not exercised. |
| Callback delivery in the bundled shape | Not verified. |
| Health check, `SIGTERM` exit 0, logs on stderr | Checked. Every `check` and `federation_check.py` run. |
| Shutdown timeouts of 15 s and 5 s | Read from configuration defaults. Stops with traffic in flight were not timed. |
| `SIGUSR1` certificate reload | Checked. `federation_check.py`, 44 of 44, [bundled-profile.md](bundled-profile.md#checking-a-deployment). |
| `SIGHUP` kills the process | Read from `src/main/signals.rs`. Not exercised. |
| One process per `data/` | Stated in [bundled-profile.md](bundled-profile.md#supervising-it). Not exercised. |
| Devices, rooms and signing keys survive restart | Checked. `check` and `federation_check.py`. |
| Queued federation delivery after a peer restart | Checked. `federation_check.py`, delivered 30 to 37 s after the peer returned. |
| Same-schema upgrade and downgrade | Checked on 2026-09-23. 1.9.0-110 to 1.9.0-160 and back on one directory, 31 of 31: devices and rooms from both binaries survived both swaps, and an unrecorded binary swap was refused. Details in the ticket notes. |
| Startup log lines for new and loaded databases | Checked on 2026-09-23, same run. |
| Migration across a schema change, refusal of a newer schema, `force_migration` | Read from `src/service/migrations/mod.rs`, `check_database_version`. Not exercised: no two releases differ in schema yet. |
| Connector-side persistence and crypto | Not this repository's to verify. |
