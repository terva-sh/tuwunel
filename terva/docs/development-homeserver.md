# Development homeserver and recovery

This recipe runs one tuwunel instance for the existing terva Matrix connector.
It does not run the connector or implement routing, client storage or crypto.
Read the [homeserver contract](homeserver-contract.md) for that boundary.
For a two-server test profile with scoped peer access, use the
[lifecycle and federation checks](lifecycle-checks.md). This single-server
recipe keeps its private-peer denylist unchanged.

The operator chose Docker Compose, a locally source-built image, loopback-only
published ports, and an isolated container network. Each instance has its own
private test CA. No command installs that CA in host trust. These decisions
preceded implementation of TKT-01M1VZ1VD (Provide a repeatable development
homeserver and recovery recipe).

## Prerequisites and limits

- macOS or Linux, a non-root operator, Git, Bash, uv with Python 3.11 or newer,
  OpenSSL 3, and a **local Docker 29 or newer daemon** with Buildx Bake and
  Compose v2 or newer.
  Remote Docker contexts and Windows are not supported by this wrapper.
- Native Linux ARM64 or AMD64 containers. Allocate at least 8 GiB to the Docker
  VM for a cold build. `JOBS=4` is the build default; use fewer jobs if it runs out
  of memory. The build needs internet access and substantial build-cache space.
- An existing private parent directory outside every Git worktree, writable by
  this UID/GID and shared with Docker. Avoid cloud-synced or network filesystems.
  The wrapper rejects symlink paths, existing destinations, and root operators.
- Disposable development data only. The leaf certificate lasts 30 days; the CA
  lasts one year. This recipe has no certificate renewal or in-place upgrade
  command. Expired certificates must never be bypassed with insecure TLS.
  Create a new disposable identity when this fixture reaches its lifetime.

The homeserver has a 2 CPU quota, 2 GiB memory limit, 512-task limit, a read-only
root filesystem, no Linux capabilities, and no privilege escalation. It runs as the
operator's numeric UID/GID. RocksDB uses bounded worker/cache settings and disables
direct I/O for Docker bind-mount compatibility. This is not a production sizing
recommendation. The log driver retains at most three 10 MiB files. Disk use by
media, the database and snapshots is not bounded; monitor the parent filesystem.
The raw-TLS HAProxy relay has a 0.25 CPU quota, 64 MiB memory limit, 32-task limit
and at most two 1 MiB logs. It also runs non-root without Linux capabilities or
privilege escalation. It mounts only its non-secret configuration, not TLS keys.

## Build and initialize

Run the examples from this repository's root. Do not use a moving upstream image
or the older podman fallback on a host with working Docker Bake.

```sh
terva/development/build.sh --print
terva/development/build.sh
```

The wrapper selects one native upstream `docker` target: release profile, default
features, Debian testing-slim, and the Rust version in `rust-toolchain.toml`.
Upstream Bake includes `direct_tls`. It installs only `cargo-chef` from the
optional Cargo tool list and avoids scheduler-policy privileges. Review the
printed plan after an upstream import.

The final line prints a tag of the form
`terva/tuwunel-dev:<full-source-commit>-arm64` or `-amd64`. `init` resolves it to a
local `sha256:` image ID, which Compose uses with pulling disabled. `init` also
pins the HAProxy relay's local image ID. The wrapper refuses uncommitted tracked
changes and untracked source/build-system files. This identifies source, not a
bit-for-bit reproducible build. Debian packages and other upstream build inputs
can change. Retain the actual images for recovery.

Choose the host label, port and paths before initialization. A host label must be
a single lowercase label under `.test`, such as `tuwunel.test` or `agentlab.test`.
The Matrix identity always includes the container's TLS port, such as
`tuwunel.test:8448`. A different published host port does not change that identity.

```sh
# Set these to an existing private parent and the tag build.sh printed.
BASE="$HOME/tuwunel-development"
IMAGE="terva/tuwunel-dev:REPLACE_WITH_SOURCE_COMMIT-arm64"
D="$BASE/original"
RELAY_IMAGE="haproxy:3.2-alpine"
docker pull "$RELAY_IMAGE"

uv run --no-project python -B terva/development/dev.py init "$D" \
  --image "$IMAGE" --relay-image "$RELAY_IMAGE" --host tuwunel.test --port 18448
uv run --no-project python -B terva/development/dev.py up "$D"
uv run --no-project python -B terva/development/dev.py status "$D"
```

`BASE` must already exist. `init` creates `D` with mode 0700 and its generated
files with mode 0600. It never overwrites an instance. Failed initialization leaves
its partial directory for inspection; choose a new destination for the next try.

`up` waits up to 120 seconds for `/_matrix/client/versions` over HTTPS with the
instance CA and hostname validation. The image's built-in health check only
probes TCP for a pure-TLS listener. Docker's `healthy` alone is not our readiness
test. Run `dev.py check "$D"` to repeat the verified HTTPS check.

No public registration, guest registration or automatic first-user admin grant
is enabled. The inline `terva` appservice can provision only its namespace and
has MSC4190 device management enabled. Its URL is omitted, making this a
polling-only fixture. Do not install another registration with the same ID/token.
Callbacks require separate connector-owned setup and a reviewed server profile.

## Endpoints, discovery and trust

For the example above:

| Consumer | Address or input |
|---|---|
| Connector/client on this host | `https://localhost:18448` |
| Test container on the reported network | `https://tuwunel.test:8448` |
| Matrix server name | `tuwunel.test:8448` |
| Public CA certificate to trust explicitly | `$D/tls/ca.crt` |
| Appservice ID and sender | `terva`, `@terva_service:tuwunel.test:8448` |
| Virtual-user namespace | `@terva_[a-z0-9_]+:tuwunel.test:8448`, anchored and escaped |
| Connector-to-server secret | `as_token` field in `$D/credentials.json` |
| Callback secret, unused in this profile | `hs_token` field in the same file |
| Source and exact local image | `$D/instance.json` |

Pass the CA through the client's explicit trust configuration. For a container,
mount only `tls/ca.crt`, read-only, and make it readable by that test process's UID.
Never mount `tls/server.key`, `ca-private/ca.key` or the full instance into a
client. Read the token from its private file through the connector's own secret
input; do not put it in shell arguments, logs, tickets or Git. The connector owns
its durable user/device mapping and storage independently of this server backup.
No per-agent password, login token or operator admin credential is needed.

To exercise DNS and certificate validation from a disposable test container,
first obtain a Python image while outside the isolated network. The verifier
resolves it to a local image ID and starts it with no published ports, only the
CA file mounted, and the operator's UID/GID. It removes the probe on exit.

```sh
docker pull python:3.13-alpine
# Run after the fixture has been seeded as shown in the recovery drill.
uv run --no-project python -B terva/development/verify.py check "$D" \
  --probe-image python:3.13-alpine
```

The probe requires the correct CA and hostname, and checks that absent CA trust
and a wrong hostname both fail. It does not install trust into either system.

```sh
curl -q --noproxy '*' --cacert "$D/tls/ca.crt" --fail --silent --show-error \
  https://localhost:18448/_matrix/client/versions
curl -q --noproxy '*' --cacert "$D/tls/ca.crt" --fail --silent --show-error \
  https://localhost:18448/.well-known/matrix/server
```

Without `--cacert`, the connection must fail certificate validation. No `-k`,
`--insecure`, host trust installation or global hosts-file edit is part of this
recipe. The certificate covers the selected `.test` hostname, `localhost` and
`127.0.0.1`.

The homeserver's only network is `internal: true`; it publishes no host ports.
Docker 29 did not publish a port directly from this internal-only network during
validation. A separate HAProxy relay therefore joins both the internal network
and a bridge used for the loopback binding. Only that relay publishes TLS, bound
to `127.0.0.1`, not `0.0.0.0` or a LAN address. Its fixed TCP backend forwards raw
TLS to `homeserver:8448`; it cannot choose an external destination or terminate
TLS. IP forwarding is disabled in the relay. The relay's bridge itself is not an
egress firewall, so keep its fixed configuration and do not put clients there.

Test containers join the internal network reported by `status`; Docker DNS
supplies the `.test` alias. Do not add another network to the homeserver or
publish additional ports. `verify.py` checks the actual Docker attachments,
resource limits and port bindings, not only the Compose YAML.

Both well-known documents advertise the internal `tuwunel.test:8448` endpoint.
Host clients use the explicit localhost URL instead of automatic discovery.
There is no listener on port 443 and no public DNS delegation. The explicit
`:8448` Matrix server name lets an internal federation client address that port
directly. Fetching discovery, federation version and signing-key documents is
not proof of a successful federated room join or event exchange.

`allow_federation` remains true, `allow_invalid_tls_certificates` remains false,
and the default private-IP denylist remains intact. `trusted_servers = []` avoids
external key-server lookups. This fixture does not enable outbound private-peer
federation. A future multi-server test must review peer-address exceptions and
peer CA trust instead of clearing the denylist or disabling certificate checks.

## Restart and inspect

```sh
uv run --no-project python -B terva/development/dev.py stop "$D"
uv run --no-project python -B terva/development/dev.py up "$D"
```

`stop` grants 60 seconds and requires an exited container with code 0 and no OOM
kill. A crash or forced kill does not qualify as a clean stop. Diagnose the cause,
start the same instance for database recovery, then stop it cleanly before backup.

`status` prints no secrets. Inspect `docker logs <reported-container>` locally
when startup fails. Logs can contain user data or operational credentials; do not
publish them without review. The wrapper suppresses subprocess error output to
avoid accidentally printing configuration secrets.

Launch configuration, TLS files, registration secrets and the Compose copy have
recorded checksums. The wrapper refuses edited launch assets rather than silently
changing identity or security settings. Use a new `init` for a different profile.
Do not invoke Compose directly, rename its container, or edit `instance.json` to
bypass a refusal.

## Offline backup and restore drill

Use this drill before relying on the recipe. `seed` creates a virtual user, a
device, a plaintext message, an encrypted-room state event and a small media file.
It does not produce or decrypt client ciphertext. `check` reads the recorded IDs
back without provisioning new identities.

```sh
uv run --no-project python -B terva/development/verify.py seed "$D" --allow-mutation
uv run --no-project python -B terva/development/dev.py stop "$D"
uv run --no-project python -B terva/development/dev.py up "$D"
uv run --no-project python -B terva/development/verify.py check "$D"
uv run --no-project python -B terva/development/dev.py stop "$D"

SNAPSHOT="$BASE/snapshot-01"
RESTORED="$BASE/restored-01"
uv run --no-project python -B terva/development/dev.py backup "$D" "$SNAPSHOT"
# Retain the exact image too. Use a new archive filename, never overwrite one.
docker image save --output "$BASE/images-01.tar" "$IMAGE" "$RELAY_IMAGE"
uv run --no-project python -B terva/development/dev.py down "$D"
uv run --no-project python -B terva/development/dev.py restore "$SNAPSHOT" "$RESTORED"
uv run --no-project python -B terva/development/dev.py up "$RESTORED"
uv run --no-project python -B terva/development/verify.py check "$RESTORED"
uv run --no-project python -B terva/development/dev.py stop "$RESTORED"
```

The image-save example assumes neither tag has moved since `init`. If either
has, use the exact `image` and `relay_image` IDs in `instance.json` as the two
arguments to `docker image save`. Keep both images for offline recovery.

The snapshot copies the entire instance, including all `/data` files and local
media, registration/configuration, TLS and CA private keys, image metadata and
fixture IDs. External media providers are not configured or supported here.
Upstream's managed database backups exclude media, which is why this recipe uses
an offline full-directory copy instead of `--restore-backup`.

`backup` requires the original container to remain present and cleanly stopped.
It checks SHA-256 hashes before/after copying and writes `snapshot.json` only on
success. Restore verifies the exact file inventory and every checksum, rejects
symlinks and special files, and creates a new private directory. It never opens
the original database or overwrites a destination. A failed copy can leave a
partial destination; preserve it for diagnosis and choose a new directory.

The original containers must be stopped and removed with `down` before restore.
Every copy of one server identity uses the same Docker container names, so Docker
refuses simultaneous launches even from different directories. The wrapper also
uses per-identity host locks under `$XDG_CACHE_HOME/tuwunel-development`, defaulting
to `~/.cache/tuwunel-development`. Run it as the same operator and Docker context.
These guards do not protect against manual Docker commands, another Docker daemon,
or an operator changing metadata. Never serve two copies of one Matrix identity.
Never launch the snapshot itself.

Retain the original and snapshot until recovery passes. `down` removes only the
stopped recipe containers and their networks, not files or images. No command prunes
Docker resources or deletes an instance. Remove disposable directories only after
reviewing their paths and deciding the data is no longer needed.

Snapshots contain usable secrets, not encrypted archives. Store them privately
and encrypt any off-host copy. Checksums detect corruption, not a malicious
snapshot author. Restore only trusted snapshots. Keep the exact image archive
and this version of `terva/development/` with the recovery record. If an image is
missing, load the archive with `docker image load --input <archive>` before restore;
restore still requires both recorded image IDs. Cross-UID, cross-architecture and
cross-host migration are outside this local recipe.

## Upgrade and rollback constraints

Do not substitute a new image into the only database copy. Database migrations
may run on startup and may not be reversible. Read upstream's release/migration
notes, take a clean full snapshot, retain the old image, and test any upgrade on a
separate recovery copy while the original is down. This wrapper deliberately
supports same-image recovery only, not an automated upgrade.

A rollback restores the pre-upgrade snapshot into another new directory and runs
its recorded old image. Never run the old image against a migrated database or
enable a force-open-newer-schema option. A rollback loses writes after its
snapshot. Coordinate connector state, devices and client crypto stores separately;
a server snapshot is not a backup of those stores. Server-name changes are not
supported migrations.

## Verification scope

Offline guard/configuration tests:

```sh
uv run --no-project python -B -m unittest discover -s terva/development -v
```

The live drill verifies explicit CA validation, discovery responses, stable server
signing keys, appservice user/device metadata, room events, encryption state and
media across restart and same-image restore. It also refuses public registration
when seeding. It is not Complement, a client crypto test, a load test or the
maintained multi-server lifecycle/federation suite. Those gates remain separate.

### Recorded run on 2026-09-06

The native ARM64 build and recovery drill passed on macOS with Docker 29.4.0,
Buildx 0.33.0, Compose 5.1.2, Python 3.13.2, uv 0.9.8 and OpenSSL 3.6.1. The server
source was `5d4866e39778492e265e6ae412943c11f9efa982`, tuwunel 1.9.0 with Rust 1.95.0.
The tested images were:

- Homeserver `sha256:791a229131280cdbe4fa28fc285a5a4e7eda152ee7ce9cb54f552f176b821950`.
- HAProxy `sha256:6343ce34a132a5dceaa24767d739df2bd519f8f7c1079ae39e4821334e8eb42e`.
- Python container probe `python:3.13-alpine`, pulled at registry digest
  `sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a`.

Both the initial fixture and its separate-directory restore passed the host and
container TLS checks. The signing keys, virtual user, device, room message,
encryption state and uploaded media survived restart and restore. Running backup,
restore before removing the original containers, and duplicate-identity startup
all refused. Original and snapshot inventories remained unchanged. Saving and
loading the exact two runtime images also passed. All recipe containers and
networks were removed afterward; private data and archives stayed outside Git.

Fifteen offline guard tests and fourteen contract request tests passed. Bash
syntax and ShellCheck 0.11.0 passed. A warm invocation of the final build wrapper
exited 0. During the cold build, an in-place wrapper edit had interrupted Bash's
final status print after Docker exported the image; the unchanged warm rerun
verified the wrapper itself. The cold source build and live server tests passed.

This run did not test AMD64, Linux-host filesystem ownership, cross-version
migration, certificate renewal, public federation or the Rust connector. At the
end of the small fixture run, Docker reported 26 homeserver tasks and about
64 MiB memory, plus 2 relay tasks and about 14 MiB. These are observations, not
capacity estimates.

The recording agent ran terva 0.133.1, commit `ea8f267`, built
`2026-09-06T06:08:39Z`, with index 0.7.0, jmap-mail 0.10.1, obsidian 0.2.0 and web
0.3.1. Only local development tools participated in this validation.
