# The bundled homeserver profile

Status: delivered by TKT-01M2ZMEZDY (Define the bundled homeserver
configuration profile). The binary it configures comes from
[bundled-binary.md](bundled-binary.md); the operator prerequisites it
implements are in [homeserver-contract.md](homeserver-contract.md).

This is the configuration a `tuwunel` runs under when terva starts it as a
native child process: one process, no reverse proxy, no container. The tooling
is `terva/bundle/bundle.py`, which creates a state directory, prints the command
a supervisor runs, and checks a deployment end to end.

## Three modes

TLS in tuwunel is a property of the whole server, not of one listener, so a
plaintext client API cannot share a process with a TLS federation listener.
That forces a choice between serving TLS and serving plaintext. A third mode
puts the plaintext server behind a reverse proxy that serves the TLS.

| | `local` (default) | `federated` | `proxied` |
| --- | --- | --- | --- |
| Purpose | terva's own agents on one machine | Agents that talk to other homeservers | The same, on a host whose reverse proxy already terminates TLS |
| Client API | Plaintext HTTP on loopback | HTTPS, same listener as federation | Plaintext on loopback; HTTPS at the proxy |
| Federation | Off | On | On, through the proxy on 443 |
| Certificates | None | Operator-supplied, or a private test CA for an isolated check | The proxy's |
| Listener | Loopback only, enforced by `init` | Any address; the operator chooses | Loopback only, enforced by `init` |

Proxied mode was added on 2026-09-30 for an internal development server, in
TKT-01M3TQ5CF81JKR5TPEFEYZF2XX (Deploy an internal dev homeserver). That host's
reverse proxy already holds the certificate and renews it. Handing the same certificate to tuwunel in federated mode would mean
two copies to renew.

```sh
terva/bundle/bundle.py init ROOT --binary PATH --server-name NAME --mode proxied \
    --public-host HOST [--peer-network CIDR ...]
```

- **The public host.** `--public-host` is the name the proxy serves on 443,
  for example `matrix.NAME`. The server publishes it in both well-known
  documents, as `HOST:443` for federation and `https://HOST` for clients. It
  answers `/.well-known/matrix/` on whatever host the request names, so the
  proxy routes that path on the server-name domain to the same loopback port.
  No separate copy of the documents is needed.
- **Peer networks.** tuwunel's default `ip_range_denylist` refuses outbound
  requests to every private range, including 10/8, 172.16/12, 192.168/16 and
  100.64/10. That includes federation, so an internal peer is unreachable
  until its network is named. Each `--peer-network` removes exactly that CIDR
  from the default list, and nothing else. It must lie inside a private range.
  Loopback, link-local and the documentation ranges stay denied whatever is
  named.
- **The client address.** tuwunel always treats a loopback peer as trusted and
  takes the client address from the forwarding headers, whatever `ip_source`
  says. So behind a proxy on the same host the logged address is the leftmost
  forwarded value, which a client can forge. The profile leaves `ip_source`
  unset rather than imply otherwise. The registration sets
  `rate_limited = false`, so agents are not limited by address in any case.
  What this affects is the address in the logs.
- **Peer keys.** Peers are asked for their own keys, because `trusted_servers`
  is empty. An internal mesh does not consult `matrix.org`.

The operator ruled on 2026-09-20 that `local` is the default and that the
server name has no default. A server name cannot change after the database is
created, and a deployment named `localhost` can never federate, so `init`
refuses to guess one. Switching modes later means editing two files and
restarting; it does not need a new database, provided the name was chosen with
federation in mind.

## Settings and why

Three files load in order, later overriding earlier, and the supervisor names
all three with `-c`:

1. `config/profile.toml`, a copy of `terva/bundle/profile/<mode>.toml`. Fixed
   policy, the same in every deployment. Copied rather than referenced so that
   upgrading the tooling cannot silently change a running server.
2. `config/tuwunel.toml`, written by `init`. The per-deployment values: server
   name, database path, listener address and port, and in federated mode the
   certificate paths and well-known documents.
3. `config/appservice.toml`, written by `init`, mode 0600. The appservice
   registration with both tokens.

| Setting | Value | Why |
| --- | --- | --- |
| `server_name` | Operator's choice, required | Permanent. See above. |
| `address`, `port` | `127.0.0.1`, default `8008` in local and proxied; operator's choice in federated | Local and proxied modes never listen off the machine. |
| `allow_federation` | `false` / `true` | The mode. |
| `allow_registration`, `allow_guest_registration` | `false` | Agents are appservice users; nobody registers. |
| `grant_admin_to_first_user` | `false` | No first user exists; do not leave a latent admin grant. |
| `allow_encryption` | `true` | Encryption is per room and must be available from day one. |
| `encryption_enabled_by_default_for_room_type` | unset, deliberately | A room is encrypted if and only if its creation asked for it. `all` or `invite` would encrypt rooms whose caller passed false. |
| `trusted_servers` | `[]` in local and proxied | A server that does not federate has no reason to contact `matrix.org`, and an internal mesh asks its peers directly. Federated mode keeps upstream's default. |
| `ip_range_denylist` | upstream's default, minus each `--peer-network`, in proxied | Internal peers are on private addresses the default refuses. |
| `allow_invalid_tls_certificates` | `false` in federated | The default, written down because the Complement configuration sets it `true` and must never be copied. |
| `dual_protocol` | `false` in federated | HTTPS only; a plaintext fallback would let a misconfigured client send a token in the clear. |
| `new_user_displayname_suffix` | `""` | Agents get the display name they asked for, not a heart. |
| `log`, `log_colors` | `info`, `false` | One line per event without ANSI, for a supervisor capturing stderr. |
| `db_cache_capacity_mb`, `db_write_buffer_capacity_mb`, `cache_capacity_modifier` | 128, 64, 0.25 | Workstation sizing. Raise for a busy deployment. |
| `device_management`, `rate_limited` | `true`, `false` | MSC4190 devices for agents; the connector is not a stranger to rate-limit. |
| `[global.well_known]` | Published host and port, federated; the public host on 443, proxied | Discovery. The domain must actually serve these or delegate to a host that does. |

Everything else keeps upstream's default. `init` also writes
`config/tuwunel-example.toml` with `--generate-config` from the same binary, so
the full schema with its defaults sits beside the deployment for reference and
so that a future tool can render configuration from the live binary rather
than from a template that drifts.

## State directory

```text
ROOT/                      0700, created by init, owned by the supervisor
  bundle.json              mode, name, address, port, binary path, sha256, version
  config/                  the loaded files and the generated example, all 0600
    identity-provider.toml written by idp; loaded fourth when present
    oidc-client-secret     the provider's client secret, placed by the operator
  data/                    database_path: RocksDB, media, signing keys
  run/                     check reports and logs; free for the supervisor's pid file
  tls/                     federated: server.crt, server.key, ca.crt for a test CA
  tls/trusted/             roots given to --trust, named by trusted_roots
  ca-private/              federated with --test-ca only: the CA key, never deployed
```

Back up `data/` and `config/` together; the database holds the signing keys
and the device records the appservice registration refers to, and neither is
useful without the other. The generated tokens exist only in
`config/appservice.toml`. Do not commit the directory anywhere.

## First run

```sh
terva/bundle/bundle.py init ROOT --binary PATH --server-name NAME [--port N]
terva/bundle/bundle.py init ROOT --binary PATH --server-name NAME --mode federated \
    --address 0.0.0.0 --port 8448 --tls-cert CERT --tls-key KEY
```

`init` refuses an existing directory, a directory inside a Git worktree, a
non-loopback address in local mode, TLS options in local mode, and a server
name that is not a lowercase hostname with an optional port. It runs the
binary once for `--version` and once for `--generate-config`, records the
binary's SHA-256 in `bundle.json`, and prints the supervisor command.

Before that command runs, the host needs one thing the profile cannot supply.
A static binary uses the system trust store for outbound TLS and, since
tuwunel 1.9.0, refuses to start without one, even in local mode with
federation off. This was reproduced: with `SSL_CERT_FILE` and `SSL_CERT_DIR`
pointed at nonexistent paths the binary exits 1 at startup naming the missing
bundle. A host with `ca-certificates` installed passes silently; a minimal host
must set `SSL_CERT_FILE` in the child's environment.

That variable locates the store; it is not how a private authority is trusted.
`SSL_CERT_FILE` replaces the platform roots, so pointing it at a fleet CA
silently breaks every public HTTPS destination. The fork adds
`[global.tls] trusted_roots`, a list of PEM files added alongside the platform
store. `init --trust PEM` copies each file under `tls/trusted/` and writes the
setting; it is valid in both modes, since outbound trust matters whenever the
server talks to anything. Roots only cover the HTTP clients; SMTP, LDAP and
S3 keep their own trust handling.

## Certificates and a test authority

A real deployment brings a certificate and key from the operator's authority
through `--tls-cert` and `--tls-key`. For fixtures, the tool carries a
test-only authority as a directory whose key never enters an instance:

```sh
terva/bundle/bundle.py ca init CADIR --name "Lab CA"
terva/bundle/bundle.py ca export CADIR --out lab-ca.crt   # the public root, for peers
terva/bundle/bundle.py ca status CADIR [CERT...]          # days left
terva/bundle/bundle.py init ROOT ... --mode federated --ca CADIR --trust peer-ca.crt
terva/bundle/bundle.py renew ROOT --pid PID              # reissue, then SIGUSR1
```

`init --test-ca` remains as a shorthand that creates such a directory inside
the instance at `ca-private/`. Certificates are issued for thirty days and the
root for ninety; `check` fails when either has under seven days left. Root
rotation is out of scope for a fixture: make a new root and new instances.

Renewal does not need a restart. With `config_reload_signal` on, its default,
`SIGUSR1` makes the server reread the certificate and key; a failed reload logs
and keeps the previous pair. The acceptors swap atomically, and connections
already open keep the certificate they negotiated.

## Signing humans in through an OpenID Connect provider

Agents are appservice users and never sign in this way. For the humans and
operators of a proxied deployment, `idp` names one OpenID Connect provider:

```sh
install -m 600 /dev/null ROOT/config/oidc-client-secret   # then write the secret into it
terva/bundle/bundle.py idp ROOT --issuer-url URL --client-id ID [--brand Authentik] [--no-registration]
```

It prints the redirect URI to register with the provider, then the new
supervisor command line, which gains a fourth `-c` for
`config/identity-provider.toml`. Update the supervisor's command to match, then
restart. The file refers to the secret by path, and `idp` refuses a secret
file that is missing, empty, or readable by anyone but the owner. The server
itself refuses to start if the file is named but missing or empty.

Proxied mode only, because the provider redirects the browser to the public
HTTPS host. What the file sets, and why:

| Setting | Value | Why |
| --- | --- | --- |
| `client_id`, `issuer_url` | from the flags | tuwunel keys every stored identity to them. `idp` refuses to change either once set. |
| `userid_claims` | `["preferred_username"]` | The Matrix localpart is the provider's username, lowercased, and nothing else. |
| `unique_id_fallbacks` | `false` | A username that is invalid or taken is an error, not a random localpart. |
| `trusted` | `false` | A provider identity never claims an account it did not create. |
| `registration` | `true`, or `false` with `--no-registration` | Sign-up through the provider ignores `allow_registration`; this flag is what gates it. With `false`, only accounts that have signed in through the provider before are admitted. |

Signing in grants no server rights: the profile sets
`grant_admin_to_first_user = false`. Promote an operator after their first
sign-in, with the server stopped:

```sh
$(terva/bundle/bundle.py command ROOT) --execute "users make_user_admin @NAME:SERVER" --execute "server shutdown"
```

Next-gen Matrix clients (MSC3861) reach the same provider through tuwunel's
own OAuth server, which `[global.well_known] client` and the provider enable
together. Legacy clients use `m.login.sso`.

A provider username inside the appservice's namespace is refused in both
flows. Upstream did not check this before the fork's fix in
TKT-01M3TZ9T038D4HZ3TPPWQG9AF3 (OIDC sign-in for the bundled profile), in
`src/api/client/session/sso.rs`.

## Supervising it

[bundled-supervision.md](bundled-supervision.md) is the handoff to terva
that puts these rules in order, with upgrades and the evidence for each.

```sh
terva/bundle/bundle.py command ROOT     # prints: tuwunel -c ... -c ... -c ...
```

`command` refuses if the binary's checksum no longer matches `bundle.json`, so
an upgrade is a deliberate step rather than something that happens because a
file changed underneath a running deployment.

| Concern | Rule |
| --- | --- |
| Health | Run the same command with `--health-check` appended. Exit 0 means the running server answered. It must share the configuration, which the three `-c` flags guarantee. In both check runs it passed one second after start on a fresh database. |
| Stop | Send `SIGTERM`. tuwunel drains client requests for up to `client_shutdown_timeout` (15 s) and federation sends for `sender_shutdown_timeout` (5 s), then exits 0. Allow 60 s before `SIGKILL`. Both check runs exited in under a second with no traffic in flight. |
| Reload | `SIGUSR1` rereads the configuration's reloadable options and the TLS certificate and key. Both are gated by `config_reload_signal`. |
| Other signals | `SIGQUIT` also shuts down cleanly. `SIGINT` shuts down unless the console feature is compiled in. `SIGHUP` is not handled and would kill the process; do not use it to reload. |
| Logs | Everything goes to stderr, one line per event. The supervisor owns rotation. |
| Restart | Same command, same directory. Devices and rooms persist; the check proves it. |
| Concurrency | One process per `data/` directory. RocksDB holds a lock file and a second process fails to open the database. |

## Checking a deployment

```sh
terva/bundle/bundle.py check ROOT
```

`check` starts the server, waits for the health check, then as an appservice
user registers an agent, creates its device, confirms `whoami` sees that
device, creates a plaintext room, confirms the room has no encryption state,
sends a message, and sees it in `/sync`. It stops the server with `SIGTERM`,
starts it again, and confirms the device and the room are still there. In
local mode it also confirms a federation request is refused. In federated mode
it confirms that `/.well-known/matrix/server` is published. In proxied mode it
confirms that the document names the public host on 443, and that the
federation API answers. It records a JSON report in
`run/check.json` and the server's output in `run/check.log`, and exits nonzero
on any failure.

For an isolated federated check with no real certificate, `init --test-ca`
generates a private CA and a certificate for the server host with a loopback
IP alternative name. `bundle.json` records `test_ca: true`; such a deployment
is a fixture, not a server anyone else can trust.

`check` runs its own server, so it cannot run while the supervised server holds
the database. For a live deployment, `probe` is a reachability check of the
discovery path, from any host, over the real names and the system trust store:

```sh
terva/bundle/bundle.py probe NAME [--cafile CA]
```

It reads both well-known documents from the server name's host on 443, then
checks `/_matrix/client/versions` and `/_matrix/federation/v1/version` at the
delegated endpoint. A `NAME` with an explicit port is not delegated, as in the
spec's discovery order: the probe skips the server document and uses that port.
It also fetches `/_matrix/key/v2/server`, which must name `NAME`, carry an
ed25519 key of the right length that has not expired, and carry a signature by
that key. It needs
no state directory and no token, and only the Python standard library.

It is not a full peer, and a pass does not prove federation works. Two steps a
peer takes are out of its reach without a dependency:

- **SRV records.** A delegation without a port is probed on 8448, where a peer
  would look up SRV records first. Delegate with an explicit port, as proxied
  mode does with `:443`, and the two agree.
- **Signature verification.** The probe checks that the key response is signed
  by one of its own keys, not that the signature is valid.

`federation_check.py`, below, is the check that two servers federate.

`python3 -m unittest` in `terva/bundle/` runs the offline tests: profile
policy, key existence against the schema, rendering of both files, and input
validation. They need no binary.

`terva/bundle/sso_check.py --binary PATH --work DIR` checks sign-in through a
provider end to end, on loopback. A stand-in OpenID Connect provider runs
inside the script over HTTPS, under a test CA the server trusts. The provider
is strict about the redirect URI, the client secret, single-use codes and PKCE.
The script plays both browser and client, and asserts:

- legacy SSO and next-gen auth each sign a user in, and a returning user
  reaches the same account;
- signing in first grants nothing, and the startup command above promotes the
  operator;
- a provider user named inside the appservice namespace gets no login token,
  with the provider untrusted and then trusted;
- `--no-registration` admits existing accounts only;
- the secret appears in no file but its own.

Complement has no SSO tests, so this is the only coverage here.

`terva/bundle/federation_check.py --binary PATH --work DIR` is the native
form of the [lifecycle and federation checks](lifecycle-checks.md): three
federated servers on loopback under three test authorities, run as plain
processes. It asserts, in order: appservice registration without a login token
and `M_USER_IN_USE` on retry; a device PUT retry that keeps one device;
namespace rejection of a local outsider, a remote identity, and the other
server's token; a plaintext room and a local send whose retry keeps its event
ID; a wrong-host certificate from a trusted authority swapped into b by reload,
which makes a's federated profile request fail, and the repaired certificate,
which makes the same request succeed; invite, join and messages in both
directions between the two servers that trust each other; refusal of an
invite to the third, untrusted server; a message accepted while b is stopped
and delivered from a's queue after b restarts on its data, without a resend;
identity, device, membership and signing keys unchanged across that restart;
and a reissued certificate presented by the same process after `SIGUSR1` with
federation continuing. It needs `init --fixture`, which empties
`ip_range_denylist` and shortens the federation timeouts and retry grace so the
queue drains inside the test window. Those values must never reach a real
deployment.

## Recorded validation on 2026-09-20

Binary: the static `1.9.0-110` artifact from [bundled-binary.md](bundled-binary.md),
on the same host.

| Deployment | Result |
| --- | --- |
| `local`, name `agents.local.test`, port 18008 | 15 of 15 checks passed |
| `federated`, name `agents.fed.test:18448`, loopback, `--test-ca` | 15 of 15 checks passed |

Both servers logged the SIGTERM receipt and the new-database migration and
nothing else at warning level. The federated server logged upstream's note
recommending a reverse proxy for direct TLS. That is a recommendation about
exposure and operations, not a defect; the bundled shape accepts it in
exchange for a single process.

On the same day, with a binary built from the `trusted_roots` and reload
change, `federation_check.py` passed 44 of 44 assertions in each of four runs
(dynamic and static binaries, before and after the review fixes), the queued
outage message arriving between 30 and 37 seconds after b restarted. An earlier ten-assertion form
of the same check passed 3 of 10 against the pre-change binary: invites and
joins over federation failed and the certificate was not reloaded, which is
the failure the change exists to remove.

Not covered here: delivery after a peer restart, which is TKT-01M2ZMEZEQ (Run
lifecycle and federation checks against the native binary); a real
certificate; any host but this one; and `SIGHUP` behaviour, which was read
from `src/main/signals.rs` and not exercised.
