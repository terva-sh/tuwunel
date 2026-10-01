# Encrypted rooms: server capabilities and connector handoff

Status: this is a server-side capability matrix and a validation handoff. It was
reviewed on 2026-09-24 against terva-main `f1d2f8ab3`, which is upstream 1.9.2
plus the fork's fixes.
Ticket: TKT-01M1QNRGR (Define encrypted-room server requirements for the Matrix
connector).

## Scope and ownership

This document extends [homeserver-contract.md](homeserver-contract.md) to
end-to-end-encrypted rooms. Read the contract first; this does not repeat its
provisioning, retry or retirement rules.

Tuwunel stores and relays key material and ciphertext produced by clients. It
never encrypts, decrypts, verifies a signature a client made, or holds a
private key. The server accepts invalid key signatures, because checking them
is the client's job. So a server that accepts `/keys/upload` or
`/sendToDevice` has shown only that it transports the request. It has not
shown that anything can be decrypted.

| Owner | Responsible for |
|---|---|
| Homeserver (this repository) | Configuration, the key, device and to-device endpoints, delivering device-list changes, key counts and to-device events, durability across restart, and federation of the same. Verified by Complement and by the server checks below. |
| Connector (`terva-conn-matrix`, on matrix-rust-sdk and `terva-sdk-rust`) | The crypto store, Olm and Megolm sessions, key sharing, device and cross-signing trust, verification, key backup policy, and choosing a receive mode. Verified by the connector's integration tests against a pinned server. |
| Operator | Configuration values, keeping the database and the connector's crypto store together, and the decision to enable sensitive traffic. |

Nothing here implements a client, an SDK or terva integration, and nothing here
claims the connector already passes any scenario.

### When encryption is required

Encryption is required per room. It must be in place before the first room that
carries sensitive traffic, or before the first federation with a server we do
not operate, whichever comes first. Plaintext and encrypted rooms coexist on the
same server and the same agent identity. Encryption is a property of the room,
not a server-wide switch. Until the handoff scenarios below pass against the
actual connector, neither trigger may be crossed.

## Evidence levels

Each row in the matrix names its evidence:

- **Complement (ours)**: the main suite reproduced by us under rootless podman on
  2026-09-23. `terva/complement/baseline.jsonl` is byte-identical to upstream's
  `tests/complement/results.jsonl`.
- **Complemau (ours)**: the appservice suite reproduced by us on 2026-09-23. It
  matches upstream's `tests/complement/complemau/results.jsonl`.
- **complement-crypto (ours)**: upstream's real-client suite, with
  matrix-rust-sdk clients built from `docker/Dockerfile.complement-crypto`'s
  pins. We reproduced it under rootless podman on 2026-09-24 with
  `terva/complement/crypto`, against tuwunel 1.9.2-177-g08b045d481. There are
  two snapshots:
  - `terva/complement/baseline-crypto.jsonl` is the run made as upstream makes
    it. It is byte-identical to upstream's `tests/complement-crypto/results.jsonl`:
    50 pass, 5 skip.
  - `terva/complement/baseline-crypto-rpc.jsonl` adds the multiprocess client
    that upstream's run never builds: 53 pass, 2 skip. It gave the same results
    twice.
- **Source**: read in the source at the revision above, not run.
- **Gap**: not covered by any of the above.

## Capability matrix

### Configuration

| Setting | Required value | Behaviour | Evidence |
|---|---|---|---|
| `allow_encryption` | `true` (the default) | When false, `PUT /send/m.room.encrypted` and `PUT /state/m.room.encryption` return 403. An `m.room.encryption` event in `createRoom`'s `initial_state` is silently dropped. Nothing stops encrypted events that arrive over federation. | Source: `src/api/client/send.rs`, `state.rs`, `room/create.rs` |
| `encryption_enabled_by_default_for_room_type` | Unset (`none`) | `all` encrypts every new room, and `invite` encrypts `private_chat` and `trusted_private_chat` rooms, even when the request asks for no encryption. The contract requires an explicit per-room choice, so leave it unset and read the state back. | Source: `apply_initial_state_pdus` |
| `one_time_key_limit` | Default 256 | Per device, across all algorithms. Uploads beyond it are dropped, and the stored keys are pruned to it when counted. | Source: `upload_keys.rs`, `service/users/keys.rs` |
| `device_key_update_encrypted_rooms_only` | Default `false` | When true, device-list changes go only to users and appservices sharing an *encrypted* room. The connector must then treat a plaintext-only peer as unknown to crypto. | Source: `service/sending/device.rs` |
| `federation_keys_timeout` | Default 8 s | Bounds federated `/keys/query` and `/keys/claim`. A server that times out is listed in `failures` and backed off on later lookups. | Source: `keys/mod.rs` |
| `allow_device_name_federation` | Default `false` | Remote servers see device IDs, not display names. | Source: `src/api/server/user.rs` |
| Appservice `device_management` | `true` | MSC4190. Creates and deletes virtual-user devices without UIA, and lets the appservice upload or replace cross-signing keys without UIA. | Complemau (ours): `...MSC4190_creates_and_updates_ghost_device`, `...MSC4190_uploads_cross-signing_master_key_without_UIA` |
| Appservice `msc3202_transaction_extensions` | `true` only in callback mode | Adds device lists, one-time key counts and fallback key types to pushed transactions. | Complemau (ours): `TestComplemauAppserviceReceivesOTKCountsAndFallbackKeys` |

No option exists for key backup, dehydrated devices, cross-signing, or
to-device limits and retention.

Because of MSC4190, anyone holding the `as_token` can replace the
cross-signing identity of every user in the namespace without further
authentication. Protect it as the root of every agent's crypto identity, not
only as an API credential.

### Key and device APIs

| Capability | Server behaviour | Evidence |
|---|---|---|
| Upload device keys | Stored for the asserted user and device only. An identical re-upload is ignored, so existing signatures survive. | Complement (ours): `TestUploadKey/*`, `TestUploadKeyIdempotency*` |
| Upload one-time and fallback keys | Duplicate key IDs are skipped. Keys are handed out in upload order (MSC4225). | Complement (ours): `TestKeyClaimOrdering`; complement-crypto (ours): `TestFallbackKeyIsUsedIfOneTimeKeysRunOut` |
| Claim keys | A one-time key first. When none are left, the fallback key is served, marked used and kept, so it can be served again. A user with nothing left is omitted from the response. Federated claims use the same path. | Source: `claim_keys.rs`; complement-crypto (ours): `TestFailedKeysClaimRetries` |
| Query keys | Local users come from the database. Remote users are always fetched over federation, and their keys are not cached. | Complement (ours): `TestFederationKeyUploadQuery/*`, `TestInboundFederationKeys` |
| Cross-signing upload | UIA is skipped on the first upload (MSC3967) and for appservices with `device_management`. Signatures upload is implemented. | Complemau (ours), as above |
| `/keys/changes` | Returns `changed`. **`left` is always empty** (a TODO). | Source: `get_key_changes.rs`; Complement (ours): `TestKeyChangesLocal` |
| Key backup (`/room_keys/...`) | Versions and keys at every scope, all stable. | Complement (ours): `TestE2EKeyBackupReplaceRoomKeyRules/*`; complement-crypto (ours): `TestCanBackupKeys` |
| Dehydrated devices (MSC3814) | **Unstable path only**, under `/_matrix/client/unstable/org.matrix.msc3814.v1/`. | Source only: gap |
| `/sendToDevice` | The transaction ID is deduplicated per sender user and device. `*` expands to every device. There is no size or count limit. | Complement (ours): `TestToDeviceMessages*`, `TestTxnIdempotencyScopedToDevice` |
| Appservice-served keys (the appservice answering claims and queries for its users) | **Not implemented.** Upstream records these as failing, and our run agrees. The connector must upload keys for each virtual device itself. | Complemau (ours): `TestComplemauAppserviceServesDeviceKeys*` and `...ServesOneTimeKeys` fail |
| Device deletion | Removes access tokens, the to-device queue, pushers, one-time keys, fallback keys, device keys and metadata, then announces the deletion as a device-list change. Cross-signing keys are untouched. | Source: `remove_device`; complement-crypto (ours): `TestRoomKeyIsCycledOnDeviceLogout` |

### Delivery to the connector

The connector chooses **one** receive mode per device and validates it.

| Signal | Sync v3 (polling with `user_id` and `device_id`) | Appservice callbacks (MSC3202 and MSC4203) |
|---|---|---|
| `device_lists.changed` | Yes, including users newly sharing a room | Yes |
| `device_lists.left` | Yes, since PR 10 | **No: always empty.** The connector must work out who left from membership events. |
| One-time key count and unused fallback types | On every response, including empty and timed-out ones. `signed_curve25519` is always present, even at zero. | Only when a transaction is built for that user. **A `/keys/claim` that drains a virtual device's keys queues no transaction**, so the count can lag until other traffic for that user arrives. |
| To-device events | From the device's inbox, `(since, next_batch]`, with no cap | Pushed as `de.sorunome.msc2409.to_device` whenever the recipient is in the namespace. No opt-in is needed, and `receive_ephemeral` does not gate it. **They are also written to the device's inbox**, which only a sync with that device ID empties. |
| Simplified sliding sync (MSC4186) | Unstable path only. Provides the `e2ee` and `to_device` extensions. | n/a |

Evidence: Complement (ours) covers `TestDeviceListUpdates/*` and
`TestSync/parallel/Device_list_tracking/*`. Complemau (ours) covers
`TestComplemauAppserviceReceivesToDevice`, `...ToDeviceManyRecipients`,
`...ToDeviceBurstExcludesUninterestedAppservice`, and
`TestComplemauDeviceListsRemoteRoomSharer`.

The callback rows marked **No** and the one-time key lag are evidence gaps for
a callback-mode connector. The unbounded inbox growth is also an operational
gap: see Restart and durability.

### Notifications

The server applies the default push rules to every event, including
`m.room.encrypted`. For an encrypted event it can match only the type, the
member count and the sender. So `.m.rule.encrypted_room_one_to_one` and
`.m.rule.encrypted` notify for every message, and the unread notification
counts in sync count every encrypted message. Mentions and keywords inside
ciphertext are invisible to the server. A connector that needs them must
evaluate the rules again after decrypting, which is what matrix-rust-sdk does
client-side. Push gateways receive `m.room.encrypted` at high priority. Agents
seldom use push gateways; this matters only if one is configured.

Evidence: source (`src/service/pusher/append.rs`, and ruma's predefined rules).
No Complement test covers notification counts for encrypted rooms: a gap.

### Restart and durability

| State | Guarantee | Evidence |
|---|---|---|
| Device keys, one-time and fallback keys, cross-signing keys, key backup | Stored in RocksDB and survive a restart with the same `database_path`. | Source |
| To-device events | Stored in the database and delivered at least once. An event is deleted only by a later sync whose `since` covers it, so a response lost in transit is sent again. No TTL or retention sweep exists. | Source: `sync/v3.rs`, `service/users/device.rs`; complement-crypto (ours): `TestToDeviceMessagesArentLostWhenKeysQueryFails` and `TestClientRetriesSendToDevice` |
| Transaction IDs for `/send` and `/sendToDevice` | Deduplicated per user and device. The contract's rule applies: not exactly once across a crash. | Complement (ours): `TestTxnIdempotencyScopedToDevice` |
| Client restart with an intact store | Supported by the above. | complement-crypto (ours, with the multiprocess client): the `/rust` subtests of `TestUnprocessedToDeviceMessagesArentLostOnRestart`, `TestRoomKeyIsNotCycledOnClientRestart` and `TestSigkillBeforeKeysUploadResponse` pass. Upstream records them as skipped only because its runner never sets `COMPLEMENT_CRYPTO_RPC_BINARY`. |
| Server restored from an older backup | **Not safe for crypto.** Clients may have used one-time keys the restored database still offers, and to-device events acknowledged after the backup are gone. Reconcile as a lost-state event (scenario 4). | Reasoning from the above; not tested |

The server cannot restore the connector's crypto store, and reusing a device ID
does not recover lost private keys. Both points are in the contract.

### Federation

| Capability | Behaviour | Evidence |
|---|---|---|
| Device-list and signing-key EDUs | Applied before the PDUs in the same transaction, so a key change the origin made before a message is not seen after it (PR 25). The origin must own the user. | Complemau (ours): `TestComplemauDeviceListsRemoteRoomSharer`, 8 of 8 |
| Join-time device list | A joiner's devices and signing keys are sent to servers new to them (PR 20). | Complement (ours): `TestDeviceListsUpdateOverFederationOnRoomJoin` |
| `m.direct_to_device` | The origin must own the sender, and `message_id` is deduplicated. Targets must be local, and active or in an appservice namespace. | Complement (ours): `TestToDeviceMessagesOverFederation/good_connectivity` |
| `/user/devices`, `/user/keys/query`, `/user/keys/claim` | Served for local users, with the fallback key on claim. | Complement (ours): `TestInboundFederationKeys` |
| Partial-state (faster) joins | Device-list and to-device handling during a partial-state join **fails** in the baseline. | Complement (ours): `TestPartialStateJoin/*` device tests fail, as upstream |
| Interrupted connectivity | `TestToDeviceMessagesOverFederation/interrupted_connectivity` is skipped upstream. `TestDeviceListsUpdateOverFederation/interrupted_connectivity` passes under Docker and fails under our podman rig because of the network, not the server. | Complement |

## Device-list defects fixed, and the trade-off carried

| Defect | Fix | Evidence |
|---|---|---|
| TKT-01M1R2N6 (Populate device_lists.left when the syncing user leaves a room) | PR 10, 121828e3f | `TestDeviceListUpdates/when_leaving_a_room_with_a_local_user` and `.../with_a_remote_user`, and their parent, moved from fail to pass |
| TKT-01M1QMK6C (Fix device list updates and remote key query over federation) | The 1.9.2 import (PR 17) for renames, and PR 20 for join-time lists | `TestFederationKeyUploadQuery` passes, and `TestDeviceListsUpdateOverFederationOnRoomJoin` moved from fail to pass (7375263e2) |
| TKT-01M38K7YVX (Join-time device-list snapshot makes a Complemau device test flaky) | PR 25: key EDUs before PDUs | `TestComplemauDeviceListsRemoteRoomSharer` 8 of 8, after PR 20's build failed it 3 of 4 |

**Carried trade-off.** An inbound device-list update that carries no keys and is
not a deletion is not reported in `device_lists.changed`. So a remote *keyless*
device's rename does not reach local clients, while every change to a device
with keys does. It is recorded in the contract. It does not affect encryption,
because a keyless device cannot receive encrypted messages.

## Handoff: scenarios for the connector

Run every scenario against the actual connector and a pinned server revision
recorded with the results, first on one server and then across two federated
servers we operate. Each scenario names what the server side guarantees and
checks, and what the connector must show. A scenario passes only when the
connector-side result is observed. A server response code proves nothing about
decryption.

### 1. Encrypted round trip
- **Setup.** Two agents A and B, each a virtual user with an MSC4190 device and
  keys uploaded by the connector. A creates a room with `encrypted: true`, reads
  back `m.room.encryption`, and invites B, who joins.
- **Steps.** A sends a message and B replies. Repeat with B on the second server.
- **Server checks.** The event stored is `m.room.encrypted`, and the server holds
  no plaintext body. B's one-time key count falls after A's claim and is
  reported through the receive mode. Room keys travel as to-device events.
- **Connector must show.** Each side decrypts the other's message, and the
  sender's device is known and trusted according to the connector's trust
  policy.

### 2. Mixed rooms
- **Setup.** The same agent is in a plaintext room P and an encrypted room E.
- **Steps.**
  - Send through the connector in both rooms.
  - Through the connector's own send API, ask for a plaintext message in E.
  - On a test server with `encryption_enabled_by_default_for_room_type` set,
    create a room with `encrypted: false`.
- **Server checks.**
  - P holds plaintext events, and E holds only `m.room.encrypted` message events.
  - The server does not refuse a plaintext event in an encrypted room; Matrix
    leaves that to clients. So "E holds no plaintext" is a check on the
    connector, made by reading E's events from the server afterwards.
  - With the default forced, the "unencrypted" room carries `m.room.encryption`.
- **Connector must show.**
  - It refuses the plaintext request for E, or encrypts it, before anything
    reaches the server.
  - It never sends plaintext into a room whose state has `m.room.encryption`.
  - It reads the state rather than trusting its own request.
  - Sensitive content is routed only to encrypted rooms.

### 3. Restart
- **Setup.** An established encrypted conversation as in scenario 1.
- **Steps.**
  - (a) Restart the connector with its crypto store intact.
  - (b) Restart the homeserver with the same database.
  - (c) Kill the connector between a key upload request and its response.
  - (d) Queue to-device events for a device while the connector is down.
- **Server checks.** Keys and queued to-device events survive (b). The events
  from (d) are delivered after restart and removed only after the next sync
  acknowledges them.
- **Connector must show.** The conversation continues without new device IDs,
  lost messages or undecryptable events. The complement-crypto restart subtests
  pass against this server with matrix-rust-sdk's own client. They say nothing
  about how the connector keeps its store or restarts its process, so this
  scenario still has to pass against the connector.

### 4. Lost state
- **Setup.** An established encrypted conversation.
- **Steps.**
  - (a) Delete the connector's crypto store for one agent and restart.
  - (b) Restore the homeserver from an older backup while the connector keeps
    its store.
- **Server checks.** In (a), `GET /devices/{id}` still returns the device, while
  its keys belong to a store that no longer exists.
- **Connector must show.** It detects the loss rather than silently re-uploading
  under the same device ID. It follows a defined recovery: a new device ID, or
  an operator-approved reset, and never reuse. Peers see a new device. Messages
  from before the loss are either recovered through key backup, if the
  connector uses it, or reported as undecryptable, never silently dropped. For
  (b), it reconciles its sync cursor and one-time keys against the restored
  server, as the contract requires.

### 5. Recipient change
- **Setup.** An encrypted room with A and B, then:
  - (a) B adds a second device;
  - (b) B's device is deleted;
  - (c) B leaves the room;
  - (d) C joins the room, from the second server.
- **Server checks.**
  - (a) and (d) reach A in `device_lists.changed`. For (d) that happens even
    when C's server shares no other room with A (PR 20).
  - (b) is announced as a deletion.
  - (c) appears in `device_lists.left` under sync v3. **In callback mode it does
    not**; see Delivery.
- **Connector must show.**
  - A's next message is encrypted to B's new device in (a) and to C in (d).
  - A rotates the room key after (b) and (c), so B's removed device and the
    departed B cannot decrypt later messages.
  - C cannot read history from before its join unless the room's history and
    key-sharing policy allows it.

### Server-side checks still to add

These belong to this repository and are not yet automated:

- `docker/Dockerfile.matrix-rust-sdk` runs matrix-rust-sdk's own integration
  tests against tuwunel, with 8 tests skipped in `docker/bake.hcl`. Upstream
  commits no results for it, so it is not evidence here until we run it.
- Add a Complement or lifecycle check that to-device events and key counts
  survive a server restart. That belongs with TKT-01M1VZ1VF (Add repeatable
  homeserver lifecycle and federation checks).
- Decide whether callback mode needs `device_lists.left`, and a transaction
  queued when one-time keys drain. If the connector uses callbacks, file both as
  server work. If it polls, record that they are not needed.
- Decide on a retention limit for to-device inboxes of devices that never sync,
  as happens in callback mode.

## Source map

| Area | Source |
|---|---|
| Encryption switches and room defaults | `src/core/config/mod.rs`; `src/api/client/room/create.rs`, `apply_initial_state_pdus`; `src/api/client/send.rs`; `src/api/client/state.rs` |
| Key endpoints | `src/api/client/keys/`; `src/service/users/keys.rs` |
| Backup and dehydrated devices | `src/api/client/backup/`; `src/api/client/dehydrated_device.rs` |
| To-device | `src/api/client/to_device.rs`; `src/service/users/device.rs`; `src/service/sending/device.rs` |
| Sync | `src/api/client/sync/v3.rs`; `src/api/client/sync/v5/extensions/` |
| Appservice transactions | `src/service/sending/sender/dispatch/appservice.rs` |
| Push rules and counts | `src/service/pusher/append.rs`, `send.rs` |
| Federation | `src/api/server/send.rs`; `src/api/server/user.rs` |
| Real-client suites | `terva/complement/crypto/`; `terva/complement/baseline-crypto.jsonl`, `baseline-crypto-rpc.jsonl`; `tests/complement-crypto/results.jsonl`; `docker/Dockerfile.complement-crypto`; `docker/Dockerfile.matrix-rust-sdk` |
