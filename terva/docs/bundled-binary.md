# Building the bundleable tuwunel binary

Status: delivered by TKT-01M2ZMEZD3 (Build a static bundleable tuwunel
binary without containers), part of the bundling decision recorded in
[adoption-plan.md](adoption-plan.md).

terva ships as one static binary. The homeserver it bundles has to be the same
shape: a single statically linked `tuwunel` executable that terva can download,
verify, and start as a child process. Upstream already publishes such binaries
for each release. This document is the recipe for producing one from
`terva-main`, so that fixes carried by this fork ship in the bundle.

## Which build path

Three build paths exist in this repository, and the host decides which one
applies.

| Host has | Use |
| --- | --- |
| A real Docker daemon with `buildx bake` | `docker/bake.hcl`, as upstream CI does. `terva/development/build.sh` wraps it for the development image. |
| podman, but root to install build packages is unavailable | `terva/build.sh`, one container image with the packages Bake installs. |
| No container runtime and no root | `terva/bundle/build.sh`, this document. |

The third path exists because the development host in September 2026 was a
Debian 13 machine with no `docker`, `podman`, `buildah` or `nix`, and no `sudo`.
Everything it needs lives under one user prefix.

## Prerequisites

The script checks every item below and stops with the name of what is missing.
It installs nothing. `PREFIX` defaults to `~/.local`.

| Need | Why | User-scope source |
| --- | --- | --- |
| rustup with the toolchain in `rust-toolchain.toml` | The build | `rustup` auto-selects it from the file. |
| `cmake` | `aws-lc-sys`, the TLS provider since 1.9.0 | `mise install cmake`; set `CMAKE` to its `bin` directory. |
| `libclang.so` | RocksDB's bindgen | The PyPI `libclang` wheel carries it at `clang/native/libclang.so`. Copy it to `$PREFIX/lib/libclang`. No `clang` binary is needed; the mise `clang` package does not ship the library anyway. |
| `liburing` headers, `.a` and `.pc` | The `io_uring` default feature | Build liburing 2.9 from source: `./configure --prefix=$PREFIX && make -C src && make install`. |
| `pkg-config` | RocksDB probes liburing with it | Build pkgconf 2.3 from source with `--prefix=$PREFIX` and symlink `$PREFIX/bin/pkg-config` to `pkgconf`. |
| system `g++` with `libstdc++.a`, `libc.a`, `libm.a` | The static link | Debian's `g++`, `libstdc++-14-dev` and `libc6-dev`. These were already present; they are the one system dependency. |

Three traps cost a build each and are handled by the script:

- A bare `libclang.so` has no resource directory, so bindgen cannot find
  `stdbool.h`. `BINDGEN_EXTRA_CLANG_ARGS` lends it gcc's include directory.

- pkgconf treats its own install prefix as a system path and silently strips
  the `-I` and `-L` flags pointing there, so RocksDB compiled without the
  liburing header. `PKG_CONFIG_SYSTEM_INCLUDE_PATH` and
  `PKG_CONFIG_SYSTEM_LIBRARY_PATH` are set to the real system paths.
- Static linking flags must not reach build scripts and proc macros. Passing
  `--target` explicitly keeps `RUSTFLAGS` off host artifacts.

## Recipe

```sh
terva/bundle/build.sh            # static binary and .sha256 into ~/.cache/tuwunel-bundle
terva/bundle/build.sh dynamic    # ordinary release build, for diagnosis
```

The feature set is upstream's `default` plus `direct_tls`, the same set every
binary upstream ships carries, so the result can serve HTTPS and federate
without a reverse proxy. The static flags mirror `static_rustflags` and
`static_libs` in `docker/bake.hcl`. The output name follows upstream's release
naming, `v<version>-release-default-<arch>-<level>-linux-gnu-tuwunel`, so a
future download recipe can treat fork and upstream artifacts alike.

`CPU=v2` or `CPU=v3` selects a higher x86-64 microarchitecture level. Only
`v1` was built and run.

Build output goes outside the tree, so a build cannot dirty the working tree.
Cargo runs with `--locked`.

## Releases

A fork release is a tag `terva-v<upstream version>-<fork commits>` on
`terva-main`, for example `terva-v1.9.0-110`, which is exactly what
`tuwunel --version` prints at that commit (the version macro only looks at
tags of the form `v<n>`, so the fork tag itself does not disturb it). Pushing
the tag runs
`.forgejo/workflows/release.yml`, which builds with this script inside the
same Rust image as CI, refuses a dirty tree or a version that disagrees with
the tag, compresses with `zstd -19`, and publishes two kinds of asset on the
Forgejo release:

| Asset | Content |
| --- | --- |
| `v<version>-release-default-<arch>-<level>-linux-gnu-tuwunel.zst` | The static binary, upstream's naming |
| `SHA256SUMS` | One line per compressed asset |

Nothing is signed. A signing key does not live in this repository; if one is
introduced it belongs to the release process, not the tree.

The same workflow runs on manual dispatch as a rehearsal: the same build,
the tag check skipped, checksums printed, nothing published. Run it from
the Actions tab or with `tea actions workflows dispatch release.yml --ref
BRANCH` before cutting a tag. On the runner the script's prerequisites are
Debian packages under `/usr`, and `liburing.pc` is found under the
multiarch `pkgconfig` directory; `REQUIRE_CLEAN=1` counts untracked files.

To fetch and verify, from terva or by hand, with `FORGE` set to the base URL
of the internal Forgejo that publishes these releases (the public mirror
publishes source only):

```sh
tag=terva-v1.9.0-110
asset=v1.9.0-110-release-default-x86_64-v1-linux-gnu-tuwunel.zst
base=$FORGE/terva-sh/tuwunel/releases/download/$tag
curl -fsSLO "$base/$asset" && curl -fsSLO "$base/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS
zstd -d "$asset" -o tuwunel && chmod 755 tuwunel
./tuwunel --version    # prints the version the tag names
```

The contract for a launcher is therefore: pick the asset by architecture and
CPU level, verify the checksum before decompressing, and treat a version
string that does not match the tag as a corrupt download. Only `x86_64-v1`
is built today; the job's matrix widens when another target has been built
and run.

## Verification

Performed on 2026-09-20 on the host described above, at fork commit
`52f619d1b` (upstream 1.9.0 plus 110 fork commits), with `CPU=v1`.

| Check | Result |
| --- | --- |
| `file` on the artifact | ELF 64-bit LSB executable, statically linked, stripped |
| `ldd` on the artifact | not a dynamic executable |
| `tuwunel --version` | `tuwunel 1.9.0-110 (v1.9.0-110-g52f619d1be-dirty)` |
| `tuwunel --generate-config` | Exit 0, a 4176-line example document |
| Boot on a loopback fixture | Listening on `tcp:127.0.0.1:18448` with direct TLS under a private CA and an inline appservice registration |
| `tuwunel --health-check` | Passed 2 s after start |
| `terva/contract/requests.sh` | `versions`, `register` twice, `put-device` twice, `whoami`, `devices`, `create-room` (plaintext), `send`, `sync`, `joined-rooms`, `invite`, `join` and `members` all succeeded; `encryption` returned the expected `M_NOT_FOUND` for a plaintext room |
| SIGTERM | Clean shutdown, exit 0, no errors or panics logged |

The fixture is the shape the bundled profile will formalise: one process, a
loopback listener, TLS served by the binary itself, and terva's agents behind
an appservice registration. That profile is TKT-01M2ZMEZDY (Define the bundled
homeserver configuration profile).

Three facts to carry forward:

- The uncompressed artifact is 97 MiB. Upstream's compressed release binaries
  are about 33 MB, so a download path will want the same `zstd` compression.
- `-dirty` in the version means the tree held uncommitted files at build time.
  A release artifact must be built from a clean, committed tree so that its
  version names a commit that exists.
- A static binary uses the host's trust store for outbound TLS. Since 1.9.0 it
  refuses to start without a CA bundle, so a bundled deployment must provide
  one or set `SSL_CERT_FILE`. This host had `/etc/ssl/certs`, so the check
  passed silently here and was not exercised.

Later the same day, after the `trusted_roots` and reload change merged, the
script rebuilt from a clean tree with `REQUIRE_CLEAN=1`: version
`1.9.0-120 (v1.9.0-120-gd409e99a7d)`, no dirty marker, 97 MiB uncompressed and
27 MiB after `zstd -19`. That binary passed the profile check in local mode,
15 of 15, and the native lifecycle and federation check, 44 of 44
([bundled-profile.md](bundled-profile.md)).

On the internal runner, the release rehearsal built the static binary from
the Debian `rust:1.95-trixie` image, verified it statically linked, and
compressed and checksummed it: 11 m 29 s alone (run 15, version `1.9.0-133`),
23 m 8 s while sharing the runner with a CI run (run 22, version
`1.9.0-134`, 96.9 MiB to 26.7 MiB). The publish step is proven by the first
real tag.

Not verified: `aarch64`, `CPU=v2` and `CPU=v3`, which are now refused off
x86-64, and the publish step.
