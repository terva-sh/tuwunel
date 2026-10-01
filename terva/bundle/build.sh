#!/usr/bin/env bash
# Build a standalone tuwunel binary for bundling with terva, without containers.
#
# This is the third build path in this repository, beside upstream's Docker
# Bake and terva/build.sh's podman fallback. It exists for hosts that have
# neither a container runtime nor root, and it produces the artifact terva
# bundles: one statically linked executable, with the same feature set as
# upstream's `default` profile plus `direct_tls`.
#
# Nothing is installed. Every prerequisite is checked and, when missing, the
# script names it and the user-scope way to get it, then stops. The recipe is
# in terva/docs/bundled-binary.md, which also records what was verified.
#
# Usage:
#   terva/bundle/build.sh            # static build into $OUT
#   terva/bundle/build.sh dynamic    # ordinary release build, for diagnosis
#
# Environment:
#   PREFIX   user prefix holding liburing and pkgconf   (default ~/.local)
#   LIBCLANG directory holding libclang.so              (default $PREFIX/lib/libclang)
#   CMAKE    directory holding the cmake binary         (default: first on PATH)
#   OUT      where the binary and checksum are written  (default ~/.cache/tuwunel-bundle)
#   TARGET   cargo target directory, outside the tree   (default $OUT/target)
#   JOBS     cargo parallelism                          (default nproc)
#   CPU      x86-64 microarchitecture level, v1..v3     (default v1)
#   REQUIRE_CLEAN=1  refuse to build from a tree with uncommitted or untracked
#            files, so the binary's version names a commit that exists
set -euo pipefail

cd "$(dirname "$0")/../.."

mode="${1:-static}"
case "$mode" in static|dynamic) ;; *) echo "usage: $0 [static|dynamic]" >&2; exit 2 ;; esac

PREFIX="${PREFIX:-$HOME/.local}"
LIBCLANG="${LIBCLANG:-$PREFIX/lib/libclang}"
OUT="${OUT:-$HOME/.cache/tuwunel-bundle}"
TARGET="${TARGET:-$OUT/target}"
JOBS="${JOBS:-$(nproc)}"
CPU="${CPU:-v1}"

arch="$(uname -m)"
case "$arch" in
    x86_64)  triple=x86_64-unknown-linux-gnu; sys="x86_64-$CPU-linux-gnu" ;;
    aarch64) triple=aarch64-unknown-linux-gnu; sys="aarch64-v8-linux-gnu" ;;
    *) echo "unsupported architecture $arch" >&2; exit 2 ;;
esac
case "$CPU" in
    v1) cpu_flag="" ;;
    v2|v3)
        # The levels are x86-64 microarchitecture levels and mean nothing on
        # another architecture, where the name would still claim them.
        [ "$arch" = x86_64 ] || { echo "CPU=$CPU is x86-64 only; this is $arch" >&2; exit 2; }
        cpu_flag="-C target-cpu=x86-64-$CPU" ;;
    *) echo "CPU must be v1, v2 or v3" >&2; exit 2 ;;
esac

missing=0
miss() { printf 'missing: %s\n  %s\n' "$1" "$2" >&2; missing=1; }

command -v cargo >/dev/null || miss cargo "install rustup; rust-toolchain.toml selects the toolchain"
if [ -n "${CMAKE:-}" ]; then export PATH="$CMAKE:$PATH"; fi
command -v cmake >/dev/null || miss cmake "mise install cmake, then set CMAKE to its bin directory"
command -v c++ >/dev/null || miss "c++" "a system g++ is required; clang is not"
[ -e "$LIBCLANG/libclang.so" ] || miss "$LIBCLANG/libclang.so" \
    "copy libclang.so from the PyPI libclang wheel (clang/native/libclang.so) into $LIBCLANG"
# A source build puts liburing.pc under lib/pkgconfig; Debian's liburing-dev
# puts it under the multiarch directory. Accept whichever exists.
pc_dir=""
for d in "$PREFIX/lib/pkgconfig" "$PREFIX/lib/$arch-linux-gnu/pkgconfig"; do
    [ -e "$d/liburing.pc" ] && { pc_dir="$d"; break; }
done
[ -n "$pc_dir" ] || miss "liburing.pc under $PREFIX/lib/pkgconfig or $PREFIX/lib/$arch-linux-gnu/pkgconfig" \
    "build liburing 2.9 from source with ./configure --prefix=$PREFIX && make install, or install liburing-dev"
if command -v pkg-config >/dev/null; then :; elif [ -x "$PREFIX/bin/pkg-config" ]; then export PATH="$PREFIX/bin:$PATH";
else miss pkg-config "build pkgconf 2.3 with ./configure --prefix=$PREFIX and symlink $PREFIX/bin/pkg-config to it"; fi
if [ "$mode" = static ]; then
    for lib in libc.a libm.a; do
        [ -e "/usr/lib/$arch-linux-gnu/$lib" ] || miss "$lib" "install libc6-dev (a system package)"
    done
    ls /usr/lib/gcc/"$arch"-linux-gnu/*/libstdc++.a >/dev/null 2>&1 || miss "libstdc++.a" "install libstdc++-*-dev (a system package)"
fi
[ "$missing" -eq 0 ] || exit 2

if [ "${REQUIRE_CLEAN:-0}" = 1 ]; then
    # Fail closed: a git that cannot answer (for instance a checkout owned by
    # another user inside a CI container) must not pass as clean. Untracked
    # files count: an untracked module or include_str! input changes the
    # binary while --version still names the tagged commit without "dirty".
    if ! dirty="$(git status --porcelain 2>&1)"; then
        echo "REQUIRE_CLEAN=1 but git status failed: $dirty" >&2
        exit 2
    fi
    if [ -n "$dirty" ]; then
        echo "REQUIRE_CLEAN=1 and the tree has uncommitted changes:" >&2
        printf '%s\n' "$dirty" >&2
        exit 2
    fi
fi

mkdir -p "$OUT" "$TARGET"

export LIBCLANG_PATH="$LIBCLANG"
# A bare libclang.so has no resource directory, so bindgen cannot find compiler
# headers such as stdbool.h. Lend it gcc's.
export BINDGEN_EXTRA_CLANG_ARGS="-I$(cc -print-file-name=include)"
export PKG_CONFIG_PATH="$pc_dir"
# pkgconf treats its own prefix as a system path and would strip the -I and -L
# flags that point at $PREFIX. Name the real system paths instead.
export PKG_CONFIG_SYSTEM_INCLUDE_PATH=/usr/include
export PKG_CONFIG_SYSTEM_LIBRARY_PATH="/usr/lib:/usr/lib/$arch-linux-gnu"
export CFLAGS="-I$PREFIX/include" CXXFLAGS="-I$PREFIX/include"
export CARGO_TARGET_DIR="$TARGET" CARGO_BUILD_JOBS="$JOBS"
export RUMA_UNSTABLE_EXHAUSTIVE_TYPES=true

rustflags="-L native=$PREFIX/lib $cpu_flag"
if [ "$mode" = static ]; then
    # Mirrors static_rustflags and static_libs in docker/bake.hcl. The explicit
    # archives keep the C++ runtime static; see the comment in
    # docker/Dockerfile.cargo about crt-static and libstdc++. Passing --target
    # keeps these flags off build scripts and proc macros, which must stay
    # dynamic.
    rustflags="$rustflags -C relocation-model=static -C target-feature=+crt-static"
    rustflags="$rustflags -C link-arg=-l:libstdc++.a -C link-arg=-l:libc.a -C link-arg=-l:libm.a"
fi
export CARGO_ENCODED_RUSTFLAGS
CARGO_ENCODED_RUSTFLAGS="$(printf '%s' "$rustflags" | sed 's/  */\x1f/g')"

echo "==> cargo build --release -p tuwunel --features direct_tls --target $triple ($mode, jobs=$JOBS)"
cargo build --locked --release -p tuwunel --features direct_tls --target "$triple"

bin="$TARGET/$triple/release/tuwunel"
# `tuwunel --version` prints "tuwunel 1.9.0-110 (v1.9.0-110-g...)": upstream's
# semantic version, then the count of fork commits past that tag. The name
# keeps both, so an artifact says which upstream release and which fork state
# it carries.
version="$("$bin" --version | awk '{print $2}')"
name="v$version-release-default-$sys-tuwunel"
[ "$mode" = static ] || name="$name-dynamic"
cp "$bin" "$OUT/$name"
(cd "$OUT" && sha256sum "$name" > "$name.sha256")

echo "==> $OUT/$name"
file "$OUT/$name"
if [ "$mode" = static ]; then
    # ldd exits nonzero for a static executable, which pipefail would turn into
    # a failed test, so ask file(1) instead and keep ldd for the diagnostic.
    if file "$OUT/$name" | grep -q 'statically linked'; then
        echo "==> statically linked"
    else
        echo "==> NOT static" >&2
        ldd "$OUT/$name" >&2 || true
        exit 1
    fi
fi
cat "$OUT/$name.sha256"
