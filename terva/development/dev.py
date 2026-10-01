#!/usr/bin/env python3
"""Local-only Compose lifecycle and offline snapshots. Requires Python 3.11+."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import time
import tomllib

HERE = Path(__file__).resolve().parent
LABEL = "sh.terva.development.root"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def run(*args):
    # Never pass credentials as arguments or relay server/config output on error.
    result = subprocess.run(args, capture_output=True, text=True)
    require(result.returncode == 0, f"{args[0]} failed; inspect it separately without publishing secrets")
    return result.stdout


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def put_json(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def safe_path(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink paths are not supported")
    parent = path if path.exists() else path.parent
    require(parent.is_dir(), "Parent directory must already exist")
    require(not inside_git(parent), "Instance and snapshot paths must be outside Git worktrees")
    return path


def new_directory(path):
    require(not path.exists(), "Destination already exists; choose a new directory")
    path.mkdir(mode=0o700)


def validate_meta(meta):
    require(meta.get("format") == 1, "Unsupported instance format")
    require(re.fullmatch(r"[a-z][a-z0-9-]{0,40}\.test", meta["host"]), "Use a single-label .test hostname")
    require(meta["server_name"] == meta["host"] + ":8448", "Server identity mismatch")
    require(type(meta["port"]) is int and 1024 <= meta["port"] <= 65535, "Port must be 1024..65535")
    for key in ("image", "relay_image"):
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", meta[key]), "Images must be immutable local image IDs")
    require(meta["uid"] == os.getuid() and meta["gid"] == os.getgid() and meta["uid"] != 0,
            "Use the same non-root host UID/GID that created this instance")
    return meta


def identity(meta):
    return "tuwunel-dev-" + hashlib.sha256(meta["server_name"].encode()).hexdigest()[:20]


def inventory(root, exclude=()):
    entries = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        require(not path.is_symlink(), "Snapshots cannot contain symlinks")
        mode = path.stat().st_mode
        require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "Snapshots contain a special file")
        if relative not in exclude:
            entries[relative] = None if path.is_dir() else digest(path)
    return entries


def inside_git(directory):
    """True when DIRECTORY sits in a Git worktree.

    Looks for a .git entry on the way up, so the answer does not depend on git
    being installed or on it being willing to read a checkout owned by another
    user; a git that errors out must not pass as "outside". git's own answer is
    then taken only when it is affirmative.
    """
    if any((p / ".git").exists() for p in (directory, *directory.parents)):
        return True
    asked = subprocess.run(["git", "-C", str(directory), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True)
    return asked.returncode == 0 and asked.stdout.strip() == "true"


def load(root):
    require(root.is_dir() and root.stat().st_mode & 0o077 == 0, "Instance directory must be private, mode 0700")
    for name in ("instance.json", "compose.yaml", "credentials.json"):
        require((root / name).is_file() and not (root / name).is_symlink(), "Invalid instance file")
    meta = validate_meta(json.loads((root / "instance.json").read_text()))
    for name in ("config", "tls", "data"):
        require((root / name).is_dir() and not (root / name).is_symlink(), "Invalid instance directory")
    actual = {"compose.yaml": digest(root / "compose.yaml"),
              "credentials.json": digest(root / "credentials.json")}
    for folder in ("config", "tls"):
        actual.update({f"{folder}/{k}": v for k, v in inventory(root / folder).items()})
    require(actual == meta["assets"], "Launch assets changed; do not edit identity, credentials, TLS or Compose in place")
    config = tomllib.loads((root / "config/tuwunel.toml").read_text())["global"]
    require(config["server_name"] == meta["server_name"], "Config server identity mismatch")
    return meta


@contextmanager
def locked(meta):
    # Serializes this wrapper across instance and snapshot directories for one UID.
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tuwunel-development"
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (cache / (identity(meta) + ".lock")).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def container(meta, root=None, relay=False):
    name = identity(meta) + ("-relay" if relay else "")
    ids = run("docker", "container", "ls", "-a", "--filter", f"name=^/{name}$", "--format", "{{.ID}}").split()
    if not ids:
        return None
    item = json.loads(run("docker", "container", "inspect", ids[0]))[0]
    require(LABEL in item["Config"]["Labels"], "Identity container is not managed by this recipe")
    if root is not None:
        require(item["Config"]["Labels"][LABEL] == str(root), "This identity belongs to another directory; stop and down it first")
    return item


def stopped(item):
    require(item is not None, "No stopped container; run up, then stop before taking a snapshot")
    state = item["State"]
    require(state["Status"] == "exited" and state["ExitCode"] == 0 and not state["OOMKilled"],
            "A clean stop with exit code 0 is required; running, crashed or killed instances cannot be snapshotted")


def compose(root, meta, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "TUWUNEL_DEV_"))}
    env.update({"TUWUNEL_DEV_" + k: str(v) for k, v in {
        "ROOT": root, "ID": identity(meta), "IMAGE": meta["image"], "HOST": meta["host"],
        "PORT": meta["port"], "UID": meta["uid"], "GID": meta["gid"], "RELAY_IMAGE": meta["relay_image"],
    }.items()})
    result = subprocess.run(["docker", "compose", "--env-file", os.devnull,
                             "--project-name", identity(meta), "-f", str(root / "compose.yaml"), *args],
                            env=env, capture_output=True, text=True)
    require(result.returncode == 0, "Compose failed; inspect Docker state/logs locally")
    return result.stdout


def request(root, meta, method, path, body=None, token=None, trust=True, raw=False):
    context = ssl.create_default_context(cafile=str(root / "tls/ca.crt") if trust else None)
    connection = http.client.HTTPSConnection("localhost", meta["port"], context=context, timeout=5)
    headers = {"Content-Type": "application/octet-stream" if isinstance(body, bytes) else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        payload = body if isinstance(body, bytes) else json.dumps(body) if body is not None else None
        connection.request(method, path, payload, headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, data if raw else json.loads(data) if data else None
    finally:
        connection.close()


def ready(root, meta, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            status, body = request(root, meta, "GET", "/_matrix/client/versions")
            if status == 200 and body.get("versions"):
                return
        except (OSError, http.client.HTTPException, ValueError):
            pass
        require(time.monotonic() < deadline, "Verified HTTPS readiness timed out; container retained for inspection")
        time.sleep(1)


def config_text(meta, credentials):
    q = json.dumps
    return f'''[global]
server_name = {q(meta["server_name"])}
database_path = "/data"
address = "0.0.0.0"
port = 8448
allow_registration = false
allow_guest_registration = false
grant_admin_to_first_user = false
allow_encryption = true
allow_federation = true
allow_invalid_tls_certificates = false
trusted_servers = []
log = "info"
log_colors = false
cache_capacity_modifier = 0.25
db_cache_capacity_mb = 128.0
db_write_buffer_capacity_mb = 64.0
db_pool_affinity = false
db_pool_workers = 8
db_pool_max_workers = 16
rocksdb_parallelism_threads = 2
rocksdb_direct_io = false

[global.tls]
certs = "/tls/server.crt"
key = "/tls/server.key"
dual_protocol = false

[global.well_known]
client = {q("https://" + meta["server_name"])}
server = {q(meta["server_name"])}

[global.appservice.terva]
sender_localpart = "terva_service"
as_token = {q(credentials["as_token"])}
hs_token = {q(credentials["hs_token"])}
device_management = true
rate_limited = false

[[global.appservice.terva.users]]
exclusive = true
regex = {q("^@terva_[a-z0-9_]+:" + re.escape(meta["server_name"]) + "$")}
'''


def init(root, image, relay_image, host, port):
    require(re.fullmatch(r"terva/tuwunel-dev:[0-9a-f]{40}-(arm64|amd64)", image), "Use the tag printed by build.sh")
    image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()
    relay_id = run("docker", "image", "inspect", "--format", "{{.Id}}", relay_image).strip()
    meta = validate_meta({"format": 1, "image": image_id, "source_tag": image, "relay_image": relay_id, "host": host,
                          "server_name": host + ":8448", "port": port, "uid": os.getuid(), "gid": os.getgid()})
    with locked(meta):
        require(container(meta) is None and container(meta, relay=True) is None, "A container already holds this identity")
        new_directory(root)
        for folder in ("config", "tls", "data", "ca-private"):
            (root / folder).mkdir(mode=0o700)
        credentials = {"as_token": secrets.token_urlsafe(48), "hs_token": secrets.token_urlsafe(48)}
        put_json(root / "credentials.json", credentials)
        (root / "config/tuwunel.toml").write_text(config_text(meta, credentials))
        shutil.copyfile(HERE / "compose.yaml", root / "compose.yaml")
        shutil.copyfile(HERE / "haproxy.cfg", root / "config/haproxy.cfg")
        ca_key, ca_cert = root / "ca-private/ca.key", root / "tls/ca.crt"
        key, cert = root / "tls/server.key", root / "tls/server.crt"
        csr, ext = root / "ca-private/server.csr", root / "ca-private/server.ext"
        run("openssl", "req", "-x509", "-newkey", "rsa:3072", "-noenc", "-sha256", "-days", "365",
            "-subj", "/CN=Terva isolated development CA", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", str(ca_key), "-out", str(ca_cert))
        run("openssl", "req", "-new", "-newkey", "rsa:3072", "-noenc", "-sha256",
            "-subj", f"/CN={host}", "-keyout", str(key), "-out", str(csr))
        ext.write_text(f"subjectAltName=DNS:{host},DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                       "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
            "-set_serial", str(secrets.randbits(128)), "-days", "30", "-sha256", "-extfile", str(ext), "-out", str(cert))
        for file in root.rglob("*"):
            if file.is_file():
                file.chmod(0o600)
        meta["assets"] = {"compose.yaml": digest(root / "compose.yaml"),
                          "credentials.json": digest(root / "credentials.json")}
        for folder in ("config", "tls"):
            meta["assets"].update({f"{folder}/{k}": v for k, v in inventory(root / folder).items()})
        put_json(root / "instance.json", meta)
    print(f"Created {root}. Secrets stay in credentials.json. Run up next.")


def snapshot(root, meta, destination):
    require(root not in destination.parents and destination not in root.parents, "Snapshot must be separate from instance")
    stopped(container(meta, root))
    require(not (root / "snapshot.json").exists(), "Reserved snapshot manifest exists in instance")
    expected = inventory(root)
    new_directory(destination)
    shutil.copytree(root, destination, dirs_exist_ok=True)
    require(inventory(destination) == expected and inventory(root) == expected, "Files changed during snapshot; do not use it")
    stopped(container(meta, root))
    put_json(destination / "snapshot.json", {"format": 1, "files": expected, "source": str(root)})
    print(f"Verified stopped snapshot at {destination}. Treat the whole directory as secret.")


def restore(source, destination):
    require(source not in destination.parents and destination not in source.parents, "Restore must use a separate directory")
    manifest = json.loads((source / "snapshot.json").read_text())
    require(manifest.get("format") == 1 and inventory(source, ("snapshot.json",)) == manifest["files"],
            "Snapshot checksum or file inventory mismatch")
    meta = load(source)
    with locked(meta):
        require(container(meta) is None and container(meta, relay=True) is None, "Stop and down the original identity before restore")
        run("docker", "image", "inspect", meta["image"], meta["relay_image"])
        new_directory(destination)
        shutil.copytree(source, destination, dirs_exist_ok=True,
                        ignore=lambda directory, names: ["snapshot.json"] if Path(directory) == source else [])
        require(inventory(destination) == manifest["files"], "Restored files do not match snapshot")
        load(destination)
    print(f"Restored to {destination}; original and snapshot retained. Run up on the restored directory.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("directory")
    create.add_argument("--image", required=True)
    create.add_argument("--relay-image", required=True, help="local HAProxy image, such as haproxy:3.2-alpine")
    create.add_argument("--host", default="tuwunel.test")
    create.add_argument("--port", type=int, default=8448)
    for command in ("up", "stop", "down", "status", "check", "backup"):
        sub = commands.add_parser(command)
        sub.add_argument("directory")
        if command == "backup":
            sub.add_argument("destination")
    recover = commands.add_parser("restore")
    recover.add_argument("directory", help="verified snapshot directory")
    recover.add_argument("destination")
    args = parser.parse_args()
    root = safe_path(args.directory)
    if args.command == "init":
        init(root, args.image, args.relay_image, args.host, args.port)
        return
    if args.command == "restore":
        restore(root, safe_path(args.destination))
        return
    require(not (root / "snapshot.json").exists(), "Never launch a snapshot; restore it into a new directory")
    meta = load(root)
    with locked(meta):
        item = container(meta, root)
        container(meta, root, relay=True)
        if args.command == "up":
            run("docker", "image", "inspect", meta["image"], meta["relay_image"])
            compose(root, meta, "up", "-d", "--pull", "never")
            ready(root, meta)
            print("Verified HTTPS readiness with the instance CA")
        elif args.command == "stop":
            require(item is not None, "No container for this directory")
            compose(root, meta, "stop", "--timeout", "60")
            stopped(container(meta, root))
            print("Stopped cleanly with exit code 0")
        elif args.command == "down":
            stopped(item)
            compose(root, meta, "down", "--timeout", "60")
            print("Removed stopped container and test network; all instance files retained")
        elif args.command == "backup":
            snapshot(root, meta, safe_path(args.destination))
        elif args.command == "check":
            ready(root, meta, timeout=5)
            print("Verified HTTPS readiness with the instance CA")
        else:
            print(json.dumps({"server_name": meta["server_name"], "endpoint": f"https://localhost:{meta['port']}",
                              "container": identity(meta), "network": identity(meta) + "-net",
                              "state": item["State"]["Status"] if item else "absent", "image": meta["image"],
                              "relay_image": meta["relay_image"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, http.client.HTTPException) as error:
        # Do not print config or credential-bearing exception values.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print(f"Refused: {message}", file=sys.stderr)
        sys.exit(1)
