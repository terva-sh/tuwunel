# Homeserver contract for the Matrix connector

Status: server-side contract, reviewed on 2026-09-06 against `c511883fa`.
Ticket: TKT-01M1QMK6E (Define the homeserver contract for the Matrix connector).

## Scope and evidence

Tuwunel supplies a reliable, configurable Matrix homeserver. The client under
test is [terva-conn-matrix](https://github.com/terva-sh/terva-conn-matrix),
terva-sh's Rust connector using matrix-rust-sdk and
[terva-sdk-rust](https://github.com/terva-sh/terva-sdk-rust) for terva protocols
and tooling. Neither sibling repository was inspected for this contract. This document specifies what the server exposes,
not SDK configuration or a claim that connector integration already works.

The earlier identity and federation proofs passed 17 and 20 curl checks
respectively. They covered virtual users, device creation, messaging, and
cross-server namespace isolation. They did not cover restart recovery, validated
federation certificates, real encryption, or the connector itself. The current
contract adds source review and locally tested request construction, not a new
live homeserver run.

A requirement below is an obligation of the deployment or connector. A source
reference describes this tuwunel revision, not a guarantee across all Matrix
servers or future upstream imports. Repeat capability checks after upgrades.

## Inputs handed to the connector

Provide one record per homeserver, through the deployment's secret mechanism:

| Input | Meaning |
|---|---|
| Client API base URL | An HTTPS origin such as `https://matrix.example.test`. This can differ from the Matrix server name. |
| Matrix server name | The immutable identity in `@user:example.test`. Keep it unchanged when moving the endpoint. |
| Appservice ID and sender user | For example `terva` and `@terva_service:example.test`. The sender is a privileged appservice principal, not an agent identity. |
| Exclusive user namespace | Anchored, escaped regex matching only the local agent IDs the operator assigns. |
| `as_token` secret | Connector to homeserver credential, shared across that registration's virtual users. It is not a per-agent token. |
| Callback URL and `hs_token` secret | Required for callback delivery. The homeserver sends `hs_token` to the callback, which must authenticate it. Omit the URL for a polling-only fixture. |
| Delivery mode | Polling per virtual device or appservice callbacks. This contract supports describing either; connector owners choose and validate their SDK path. |
| Stable user/device mapping | Full Matrix user ID plus a nonempty device ID per logical agent device. The connector persists this mapping. |
| Trust and feature profile | CA trust, federation policy, allowed room versions, encryption support, and server revision tested. |

An ordinary agent needs neither a password nor an admin token. Keep an operator
admin account and its credentials separate from the connector. Never grant admin
status to the appservice sender or virtual agents. Namespace access already lets
the connector act as every local user in its registration; it is not an isolation
boundary between agents sharing the same token.

## Operator prerequisites

Before handing over endpoints or tokens:

1. Choose `server_name` before initializing the database. Retain the entire
   `database_path`, including media and database-held server state, across restarts.
   Preserve registration files/configuration and secrets separately. Changing the
   server name is not an endpoint move and is not a supported identity migration.
2. Install exactly one source for each appservice ID. Use inline TOML, a YAML file
   under `appservice_dir`, or an admin-registered database entry. Do not install
   the same ID in multiple sources. The YAML loader reads only `.yaml` and `.yml`
   files. Inline and directory registrations load at startup; plan a restart for
   changes rather than relying on configuration reload.
3. Set `device_management = true`. Keep `allow_registration = false` and
   `allow_guest_registration = false` unless unrelated users need registration.
   Appservice registration bypasses the public registration gate and UIAA.
   `allow_room_creation = false` does not block appservices or administrators.
4. Set `allow_encryption = true`. Leave
   `encryption_enabled_by_default_for_room_type` unset for explicit per-room
   choice. Values `all` and `invite` can encrypt rooms even when the request omits
   encryption state. A connector must read back room state before sending.
5. Supply HTTPS for the client API and for federation when enabled. A reverse
   proxy may terminate TLS; built-in TLS requires the `direct_tls` build feature.
   Bind plaintext listeners to loopback or an isolated proxy network. Expose only
   required routes and do not publish administration endpoints without access
   controls. Configure proxy timeouts to exceed `/sync`'s long-poll timeout.
6. For federation, keep `allow_federation = true`, make server-name discovery
   resolve to the intended listener, and validate certificates on both sides.
   `[global.well_known].client` publishes the client URL and `.server` publishes
   a federation `host:port`; the server-name domain must actually serve those
   discovery documents. DNS, delegation, firewall and CA trust are operator work.
7. Size CPU, memory, disk, file descriptors and process/thread limits for the
   selected build. The earlier Linux podman proof needed a limit above 2048 tasks;
   `--pids-limit 0` worked there. That is an observation, not a universal sizing
   recommendation or a requirement to remove limits on production hosts.
8. Arrange an independent operator recovery path, tested server backups and
   version-aware restore procedures. Those deployment procedures are tracked in
   TKT-01M1VZ1VD (Provide a repeatable development homeserver and recovery recipe).

### Example registration fragment

This is an inline registration fragment to merge into an operator-owned config,
not a complete launch recipe. Replace both token placeholders with distinct,
random secrets outside Git. Adapt the example namespace to the actual server.
TOML has no `null` value, so omitting `url` disables callbacks.

```toml
[global.appservice.terva]
sender_localpart = "terva_service"
as_token = "REPLACE_WITH_AS_SECRET"
hs_token = "REPLACE_WITH_HS_SECRET"
device_management = true
rate_limited = false
# For callbacks, configure an address reachable from the homeserver:
# url = "https://connector.example.test/matrix-appservice"
# msc3202_transaction_extensions = true
# receive_ephemeral = true

[[global.appservice.terva.users]]
exclusive = true
regex = '^@terva_[a-z0-9_]+:example\.test$'
```

Namespace regex matching is otherwise unanchored and users/aliases match
case-insensitively. Use lowercase localparts and nonoverlapping exclusive ranges.
The loader has a TODO for exclusive-namespace collision checks, so operator review
is required. The sender matches independently of the namespace regex. Every
homeserver hosting agents needs its own registration and distinct credentials;
a regex cannot grant authority over another server's users.

### Test-only configuration

Do not copy Complement configuration into an operator deployment. In particular,
`allow_invalid_tls_certificates = true` disables outbound certificate checks,
and `ip_range_denylist = []` removes protections against connections to private
and other reserved networks. An isolated federation fixture may need a reviewed
network exception; it is not a reason to remove the production denylist.
`trusted_servers = []` avoids external trusted-key-server lookups in an isolated
fixture, but is not a federation peer allowlist or a certificate setting.
Public registration and shared-secret registration are unnecessary for the
appservice requests below. Do not give the connector `registration_shared_secret`.

## Operation classification

Paths use `/_matrix/client/v3` unless written in full. Appservice requests use
`Authorization: Bearer <as_token>` plus URL-encoded `user_id` and, once created,
`device_id`. Omit `device_id` when creating that device. Send JSON bodies with
`Content-Type: application/json`. Never place access tokens in query strings.

| Operation | Interface and authority | Contract |
|---|---|---|
| Identify server | Public `GET /_matrix/client/versions` | Records advertised versions, not proof of appservice features or federation readiness. |
| Install/remove registration | Operator configuration or `!admin appservices register/unregister` in the admin room/console | No generic appservice-registration HTTP admin endpoint is assumed. |
| Register local virtual user | Appservice `POST /register` | `type: m.login.application_service`, explicit lowercase username, `inhibit_login: true`, no password. Returns user ID, no device or access token. |
| Assert current principal | Appservice `GET /account/whoami` | Echoes the asserted user/device. Without a device assertion this does not prove that the user exists. |
| Create/update device | Appservice `PUT /devices/{deviceId}` | MSC4190 creates on missing metadata with 201; updates existing metadata with 200. No login token is minted. |
| Inspect devices | Appservice `GET /devices` or `/devices/{deviceId}` | Device metadata for the asserted local user. Not a list of all agents. |
| Create room | Appservice `POST /createRoom` | Explicit encryption parameter translated to initial state; persist returned room ID. Room naming/routing policy is connector-owned. |
| Invite/join/leave | Appservice `POST /rooms/{roomId}/invite`, `/join/{roomId}`, `/rooms/{roomId}/leave` | Normal room authorization applies. A remote user joins through its own homeserver. An invite is not proof of a join. |
| Inspect room membership | Appservice/member `GET /joined_rooms`, `/rooms/{roomId}/joined_members` or `/members` | Membership visibility is subject to Matrix authorization. It does not prove an agent process is reachable. |
| Read state/events | Appservice/member `GET /rooms/{roomId}/state/...`, `/sync`, `/rooms/{roomId}/messages` | Process as the participating virtual user, not via an admin visibility bypass. |
| Send room event | Appservice/member `PUT /rooms/{roomId}/send/{eventType}/{txnId}` | Stable transaction ID and device assertion on retry. Acceptance is not remote delivery or decryption confirmation. |
| Keys and to-device messages | Appservice/device `/keys/upload`, `/keys/query`, `/keys/claim`, `PUT /sendToDevice/{eventType}/{txnId}` | Store and transport client-produced keys/ciphertext. The connector owns crypto, verification and key sharing. |
| Delete device(s) | Appservice `DELETE /devices/{deviceId}` or `POST /delete_devices` | MSC4190 bypasses UIAA. Destructive; not a restart operation or guaranteed cryptographic erasure. |
| List local accounts | Operator `GET /_synapse/admin/v2/users` or `/v3/users` | Implemented admin API, with administrator authorization. Do not distribute an admin token just for connector discovery. |
| Deactivate account | Operator `POST /_synapse/admin/v1/deactivate/{userId}` or admin user command | Different cleanup behavior; see retirement below. Client `/account/deactivate` still requires UIAA and is not the passwordless path. |
| Move endpoint, rotate registration, restore data | Operator | Coordinate with the connector; not an agent API operation. |

The implemented Synapse-admin subset is listed in
[`docs/development/compliance/synapse-admin.md`](../../docs/development/compliance/synapse-admin.md).
Server administrators have access to room-state/message endpoints that bypass
normal visibility. This contract forbids using them for agent auditing, logging,
search, discovery through message bodies, or receipt confirmation. Such features
must run inside a participating room member, and encrypted content stays client
owned. Presence and room membership are not a cross-server agent directory or
application-level health check.

## Provisioning, retries, and durable state

Provision a user once with an explicit ID. A repeated `/register` returns
`M_USER_IN_USE`, not a second successful creation. Reconcile that result against
the connector's durable assignment and operator-controlled exclusive namespace;
do not silently adopt an unrelated user. `/whoami` alone cannot establish prior
registration because appservice auth accepts a matching user assertion without
checking account existence. `PUT /devices/{id}` does require the user to exist.

Use a nonempty, stable device ID. On a first provisioning attempt, a missing
device can be created. On a restart, GET the expected device first. An unexpected
404 is lost or changed server state, not permission to recreate it automatically.
Re-PUT of an existing device updates display name and last-seen metadata without
replacing keys; omitting `display_name` can clear that metadata. A missing device
asserted in the query is rejected before the handler, so create it using only
`user_id`. MSC4326 uses the unprefixed `device_id` query key; this revision also accepts
`org.matrix.msc3202.device_id`. `/versions` does not advertise MSC4190/MSC4326
flags here, so check actual device operations with a disposable identity.

`POST /createRoom` has no transaction ID. Persist the returned room ID before
further work. If its response is lost, reconcile the connector's record and room
membership/state; do not blindly create a second room. An alias or room name is
not by itself a unique logical-task identifier. For membership operations, inspect
the resulting membership after uncertain responses rather than treating every
error as success.

For `/send` and `/sendToDevice`, allocate transaction IDs unique across operations
for the same user and device, not just within a room. This implementation's
transaction table keys are user, optional device, and transaction ID. Reuse the
same ID and unchanged body for a retry of the same operation. Persist the ID
before sending and retain it across restarts. The event and transaction-record
writes are separate in this revision; do not promise exactly-once delivery across
a crash. Consumers should tolerate duplicate events at the application boundary.

| State | Owner and restart rule |
|---|---|
| Accounts, device metadata, room events/membership, transaction records | Homeserver database. Restart with the same data and identity; never initialize an empty volume as recovery. |
| File/inline registrations, tokens, certificates and configuration | Operator. Restore consistently with the database and connector secrets. Database registrations live in the server database. |
| Agent mapping, room IDs, pending transaction IDs | Connector. Resume existing identities and reconcile uncertain operations. |
| Sync cursor and processed events | Connector. Persist processed state before advancing `next_batch`; do not discard it on a network timeout. Handle limited timelines/backfill. |
| Crypto store, private keys, sessions and trust | Connector/matrix-rust-sdk. Server backups cannot restore this state. Reusing a device ID is not recovery of lost private keys. |

Treat 401/403 as credentials, namespace or authorization failures to diagnose, not
as a request to register again. Handle 429 according to `retry_after_ms`; retry
network errors and eligible 5xx responses with bounded backoff and the original
transaction ID where supported. Stop retrying on an operator-defined deadline.
A restored older database can invalidate the connector's assumptions about sync
cursors and pending sends; coordinated recovery must reconcile both stores.

## Room encryption and delivery

The room-creation input includes an explicit boolean `encrypted`. True adds:

```json
{"type":"m.room.encryption","state_key":"","content":{"algorithm":"m.megolm.v1.aes-sha2"}}
```

False omits that state event; it does not override a server default that forces
encryption. Read back `m.room.encryption` after creation. A true request must
observe the expected algorithm. For a false request, an existing encryption event
means the room is encrypted and the connector must not send plaintext. Do not
downgrade on an error. Matrix can enable encryption later, but earlier plaintext
history remains exposed. Our contract therefore decides encryption at creation.
Mixed encrypted and plaintext rooms are supported at the contract level.

Polling uses `/sync?user_id=...&device_id=...&since=...`. The device assertion is
necessary for that device's to-device events and key counts. Keep the cursor
opaque even though this server currently parses a count. A full sync is not an
unbounded replay of all room history.

For callback delivery, configure a reachable appservice URL and authenticate
incoming transactions with `hs_token`. The server also appends the legacy
`access_token` query parameter to callback requests, so redact callback URL query
strings in proxies and logs. `msc3202_transaction_extensions = true` enables
additional device-list/key-count fields; the YAML spelling is `org.matrix.msc3202`.
`receive_ephemeral` separately requests ephemeral events. An absent callback URL
causes outbound appservice requests to be skipped, not deferred until a URL is
configured. Connector owners must validate durable transaction acknowledgement,
deduplication and crypto bookkeeping for their chosen receive mode. Do not assume
polling and callbacks are interchangeable SDK features or consume both blindly.

The identity proof's key endpoints accepted requests; it did not demonstrate
valid key signatures or decryption. Encrypted-room readiness was gated on
TKT-01M1R2N6 (Populate device_lists.left when the syncing user leaves a room) and
TKT-01M1QMK6C (Fix device list updates and remote key query over federation),
both done on 2026-09-23 with their Complement tests passing, and it stays gated
on real connector tests. One server behaviour from that fix affects clients.
An inbound device-list update that carries no keys and is not a deletion
describes a keyless device, and is not reported in `device_lists.changed`. So a
remote keyless device's rename does not reach local clients, while every change
to a device with keys does. The capability matrix and the crypto validation
handoff are in [encrypted-rooms.md](encrypted-rooms.md), from TKT-01M1QNRGR
(Define encrypted-room server requirements for the Matrix connector). Do not
send sensitive traffic or federate to a server we do not operate before its
scenarios pass against the connector.

## Retirement and credential rotation

Retirement is explicit destructive work, never startup reconciliation:

1. Stop the agent's sends and mark its assignment retired in connector state.
2. As the virtual user, enumerate joined rooms and invitations from member-visible
   state. Leave rooms and reject invites; inspect results before proceeding.
   Consider room ownership/power-level transfer first. Do not delete shared rooms.
3. Delete the device or devices through MSC4190 without asserting the device being
   removed. This removes device metadata, associated tokens, queued to-device
   events, one-time and fallback keys, and device keys (corrected on 2026-09-24;
   one-time keys were a TODO at `c511883fa`). Cross-signing keys remain. Do not
   promise cryptographic erasure or reuse retired device IDs.
4. If account deactivation is required, request operator action. The Synapse-admin
   deactivation handler calls `users.deactivate_account`, not `full_deactivate`:
   it removes devices and disables ordinary login, but does not leave rooms.
   `erase: true` there marks the user erased; it is not an erasure of federated
   history. The admin-room `users deactivate` path performs fuller cleanup unless
   `--no-leave-rooms` is used. Neither substitutes for a verified retirement plan.

There is no per-agent `as_token` to revoke. Appservice auth checks the registration
namespace and an asserted device's existence; it does not check the account's
ordinary-login deactivated flag. A still-authorized connector can create a new
device for an existing virtual user. Normal passwordless virtual users also have
an empty password representation, so ordinary account deactivation flags are not
a reliable agent-liveness signal. Do not claim account deactivation removes
namespace authority. Revoking a compromised appservice requires operator removal
or rotation of its registration and review of every identity it controls.

For file/inline registration rotation, quiesce the connector, change secrets in
both places and restart the homeserver with the same data. Verify the new token
works and the old one fails on an authenticated route. Coordinate callback token
changes and in-flight transactions; there is no dual-token overlap guarantee.

For database registrations, this revision **rejects duplicate registration IDs**.
Register is not an upsert; `docs/appservices.md` says so since PR 29.
Unregistering then registering requires an operator maintenance window and cancels
pending appservice deliveries. Capture needed state securely, reconcile missed
events, and test the old-token rejection. Config/file registrations cannot be
unregistered through the admin command. Do not run `show-config` into ordinary
logs because it prints registration secrets.

## Requests, handoff, and remaining validation

[`../contract/requests.sh`](../contract/requests.sh) is a one-request-at-a-time
curl cookbook for a disposable development server. It has no sync loop, crypto,
retry manager, persistent state, or terva integration. Its comments and `help`
output list required inputs. Mutations require an explicit opt-in, and deletion
requires the exact user/device pair. It does not install a server or clean up
accounts/rooms automatically. Use only synthetic content.

With `HS`, `AS_TOKEN`, `LOCALPART`, `USER_ID`, and `DEVICE_ID` supplied securely:

```sh
terva/contract/requests.sh versions
ALLOW_MUTATION=1 terva/contract/requests.sh register
ALLOW_MUTATION=1 terva/contract/requests.sh put-device
terva/contract/requests.sh whoami
terva/contract/requests.sh get-device
ALLOW_MUTATION=1 ENCRYPTED=false terva/contract/requests.sh create-room
```

Record the returned `room_id` as `ROOM_ID`. Provision a second virtual user/device
by repeating the first steps with its own identifiers; set `INVITEE` to its full
user ID, invite as the creator, then join as the invitee. Do not reuse the first
agent's asserted identity when accepting the invitation.

```sh
ALLOW_MUTATION=1 terva/contract/requests.sh invite
# Switch USER_ID and DEVICE_ID to the invited identity before joining.
ALLOW_MUTATION=1 terva/contract/requests.sh join
terva/contract/requests.sh members
terva/contract/requests.sh encryption
# Only M_NOT_FOUND for absent encryption state, not arbitrary failure, is expected
# for a known joined plaintext room. Confirm the state before this synthetic send.
ALLOW_MUTATION=1 PLAINTEXT_ROOM="$ROOM_ID" TXN_ID=probe-001 terva/contract/requests.sh send
terva/contract/requests.sh sync
```

`encryption` returns curl failure status for HTTP errors; inspect the Matrix error
body. It does not convert permission/network errors to "unencrypted". `send`
always uses a fixed synthetic message and requires confirmation of the plaintext
room ID. Repeating it with the same identity, device and transaction ID tests a
sequential retry, not crash atomicity. The helper never generates crypto keys.

Before connector-owned integration, hand over this contract, the operator's
per-server input record and a pinned server version. Validate against the actual
connector: identity/device persistence, chosen receive mode, mixed-room behavior,
namespace refusal, token rotation, and real encrypted messages. Terva routing and
room-lifecycle policy remain with connector owners.

Maintained server restart and federation checks, including trusted test
certificates and peer outages, were added in TKT-01M1VZ1VF (Add repeatable
homeserver lifecycle and federation checks); see
[lifecycle-checks.md](lifecycle-checks.md). The appservice Complement baseline
was reproduced in TKT-01M1VZ1VG (Reproduce the separate appservice Complement
baseline). The connector handoff, `connector-handoff.md`, an internal
document, orders what the connector's owners validate next and lists what is still unproven on the server.

## Local validation

Run the request tests without a homeserver. They require Python 3.11 or later,
Bash, jq and curl 7.87 or later:

```sh
bash -n terva/contract/requests.sh
uv run --no-project python -B -m unittest discover -s terva/contract -v
```

The 14 tests use a loopback HTTP recorder to check methods, encoded paths and
queries, JSON bodies, device assertions, mutation/deletion guards, header handling,
redirect refusal, and HTTP-error propagation. They also parse the TOML and JSON
examples and check document links. They do not emulate Matrix authorization or
validate tuwunel behavior. On this macOS checkout they passed with Bash 3.2.57 and
curl 8.7.1; ShellCheck 0.11.0 also passed. No new tuwunel build or live server run
was performed for this contract.

## Source map

Paths are relative to the repository root. Review these when importing upstream.

| Claim | Source and symbol |
|---|---|
| Namespace, sender and device assertions | `src/api/router/auth/appservice.rs`, `auth_appservice`; `src/service/appservice/registration_info.rs`, `is_user_match` |
| Token lookup and account gates | `src/api/router/auth.rs`, `auth`, `find_token` |
| Registration gates and repeated registration | `src/api/client/register/register.rs`, `gate_registration_allowed`, `resolve_registration_user_id`, `register_route` |
| Whoami does not establish appservice-user existence | `src/api/client/account/whoami.rs`, `whoami_route` |
| No appservice login with MSC4190 | `src/api/client/session/appservice.rs`, `handle_login` |
| Device creation/update/delete | `src/api/client/device/devices_device.rs`; `src/service/users/device.rs`, `create_device`, `remove_device` |
| Registration sources, collisions, rotation | `src/service/appservice/mod.rs`, `load`, `load_appservice`, `unregister_appservice`; `src/admin/appservice/register.rs` |
| Callback behavior and token query | `src/service/appservice/request.rs`, `send_request`, `add_access_token_query` |
| Encryption defaults and room creation | `src/api/client/room/create.rs`, `apply_initial_state_pdus`, `can_create_room_check` |
| Sync and transaction durability boundary | `src/api/client/sync/v3.rs`, `sync_events_route`; `src/api/client/send.rs`, `send_message_event_route`; `src/service/transaction_ids/mod.rs` |
| Admin authorization and deactivation | `src/api/client/admin/mod.rs`, `require_admin`; `src/api/client/admin/users/deactivate_account.rs`; `src/service/users/mod.rs`, `deactivate_account` |
| Full cleanup and UIAA distinction | `src/service/deactivate/mod.rs`, `full_deactivate`; `src/admin/user/mod.rs`, `deactivate_user`; `src/api/router/auth/uiaa.rs`, `auth_uiaa` |
| Config and advertised features | `src/core/config/mod.rs`, `AppService`, `Config`; `src/api/client/versions.rs`, `UNSTABLE_FEATURES` |
