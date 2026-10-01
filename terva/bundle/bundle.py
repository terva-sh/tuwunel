#!/usr/bin/env python3
"""Bundled tuwunel profile: initialise a state directory, print the supervisor
command, and check a deployment end to end. Generated state stays outside Git.

  bundle.py init ROOT --binary PATH --server-name NAME [--mode local|federated|proxied]
  bundle.py command ROOT
  bundle.py check ROOT
  bundle.py probe SERVER_NAME

The profile is documented in terva/docs/bundled-profile.md.
"""

import argparse
import base64
import calendar
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import tomllib
from urllib.parse import quote, urlencode

HERE = Path(__file__).resolve().parent
PROFILES = HERE / "profile"
FORMAT = 1
MODES = ("local", "federated", "proxied")
# tuwunel's default ip_range_denylist (src/core/config/mod.rs,
# default_ip_range_denylist). Outbound federation to these is refused unless a
# deployment names a peer network, which removes exactly that range.
DEFAULT_DENYLIST = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                    "192.0.0.0/24", "169.254.0.0/16", "192.88.99.0/24", "198.18.0.0/15", "192.0.2.0/24",
                    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "::1/128", "fe80::/10", "fc00::/7",
                    "2001:db8::/32", "ff00::/8", "fec0::/10")
# Ranges a peer network may open. Loopback, link-local, multicast and the
# documentation ranges stay denied whatever a deployment says.
PEER_RANGES = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")
FILES = ("profile.toml", "tuwunel.toml", "appservice.toml")
# Written by the idp verb and passed as a fourth file when present.
IDP_FILE = "identity-provider.toml"
CLIENT_ID = re.compile(r"[A-Za-z0-9._~-]{1,128}")
PREFIX = "/_matrix/client/v3"
SERVER_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*(:[0-9]{1,5})?$")
LOCALPART = re.compile(r"^[a-z0-9_]+$")
# tuwunel handles SIGTERM as a clean shutdown bounded by client_shutdown_timeout
# (15 s) plus sender_shutdown_timeout (5 s); a supervisor should allow this much
# before escalating. See terva/docs/bundled-profile.md.
STOP_GRACE = 60
HEALTH_WAIT = 60


def require(condition, message):
    if not condition:
        raise ValueError(message)


def quoted(value):
    # JSON string syntax is valid TOML basic-string syntax for these inputs.
    return json.dumps(value)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_root(value):
    path = Path(os.path.abspath(Path(value).expanduser()))
    require(not any(p.is_symlink() for p in (path, *path.parents)), "Symlink paths are not supported")
    parent = path if path.exists() else path.parent
    require(parent.is_dir(), "Parent directory must already exist")
    require(not inside_git(parent), "The state directory must be outside Git worktrees")
    return path


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


def split_name(server_name):
    host, _, port = server_name.partition(":")
    return host, (int(port) if port else None)


def deny_except(addresses):
    """The whole address space except these exact IPv4 hosts, as CIDR blocks."""
    networks = [ipaddress.ip_network("0.0.0.0/0")]
    for address in addresses:
        host = ipaddress.ip_network(address + "/32")
        networks = [part for network in networks for part in
                    (network.address_exclude(host) if host.subnet_of(network) else [network])]
    return [str(n) for n in networks] + ["::/0"]


def denylist_without(peers):
    """The default denylist with each peer network carved out of it, in any
    order: a piece left by an earlier, narrower peer is dropped whole when a
    later peer contains it."""
    def carve(network, peer):
        if network.version != peer.version:
            return [network]
        if peer.subnet_of(network):
            return list(network.address_exclude(peer))
        if network.subnet_of(peer):
            return []
        return [network]

    networks = [ipaddress.ip_network(n) for n in DEFAULT_DENYLIST]
    for peer in (ipaddress.ip_network(p) for p in peers):
        networks = [part for network in networks for part in carve(network, peer)]
    return [str(n) for n in networks]


def inside_peer_range(cidr):
    network = ipaddress.ip_network(cidr)
    return any(network.version == r.version and network.subnet_of(r)
               for r in (ipaddress.ip_network(p) for p in PEER_RANGES))


def config_files(root):
    names = FILES + ((IDP_FILE,) if (root / "config" / IDP_FILE).is_file() else ())
    return [root / "config" / name for name in names]


def command(root, binary):
    argv = [str(binary)]
    for path in config_files(root):
        argv += ["-c", str(path)]
    return argv


def deployment_text(meta, root):
    lines = ["# Written by bundle.py init. Per-deployment settings; edit and restart.",
             "[global]",
             f"server_name = {quoted(meta['server_name'])}",
             f"database_path = {quoted(str(root / 'data'))}",
             f"address = {quoted(meta['address'])}",
             f"port = {meta['port']}"]
    if meta.get("fixture"):
        lines += ["",
                  "# TEST ONLY. Lets servers on this machine federate over 127.0.0.1 and makes",
                  "# federation retries happen within a test window. Every other address,",
                  "# private or public, stays denied. The default timings are production",
                  "# backoff; never carry these into a real deployment.",
                  "ip_range_denylist = " + json.dumps(deny_except(["127.0.0.1"])),
                  "request_conn_timeout = 2",
                  "federation_timeout = 3",
                  "sender_timeout = 5",
                  "sender_retry_grace = 2",
                  "request_idle_timeout = 2"]
    if meta["mode"] == "proxied":
        if meta.get("peer_networks"):
            lines += ["",
                      "# Internal networks other homeservers federate from. Each is removed from",
                      "# tuwunel's default denylist, and nothing else is: " + ", ".join(meta["peer_networks"]) + ".",
                      "ip_range_denylist = " + json.dumps(denylist_without(meta["peer_networks"]))]
        lines += ["",
                  "# The proxy serves this host on 443 and forwards here. The server answers",
                  "# these at /.well-known/matrix/ for the server-name domain too.",
                  "[global.well_known]",
                  f"client = {quoted('https://' + meta['public_host'])}",
                  f"server = {quoted(meta['public_host'] + ':443')}"]
    trusted = [str(root / "tls" / "trusted" / name) for name in meta.get("trusted_roots", [])]
    if meta["mode"] == "federated" or trusted:
        lines += ["", "[global.tls]"]
    if trusted:
        lines += ["# Private authorities trusted for outbound HTTPS, added to the platform store.",
                  "trusted_roots = [" + ", ".join(quoted(p) for p in trusted) + "]"]
    if meta["mode"] == "federated":
        host, port = split_name(meta["server_name"])
        published = meta["server_name"] if port else f"{host}:{meta['port']}"
        lines += [f"certs = {quoted(str(root / 'tls' / 'server.crt'))}",
                  f"key = {quoted(str(root / 'tls' / 'server.key'))}",
                  "",
                  "# Discovery documents. The server-name domain must actually serve these",
                  "# or delegate to a host that does; that is operator work.",
                  "[global.well_known]",
                  f"client = {quoted('https://' + published)}",
                  f"server = {quoted(published)}"]
    return "\n".join(lines) + "\n"


def appservice_text(meta, as_token, hs_token):
    namespace = "^@" + re.escape(meta["namespace_prefix"]) + "[a-z0-9_]+:" + re.escape(meta["server_name"]) + "$"
    return "\n".join([
        "# Written by bundle.py init. Holds the appservice secrets; keep it 0600.",
        f"[global.appservice.{meta['appservice_id']}]",
        f"sender_localpart = {quoted(meta['sender_localpart'])}",
        f"as_token = {quoted(as_token)}",
        f"hs_token = {quoted(hs_token)}",
        "device_management = true",
        "rate_limited = false",
        "",
        f"[[global.appservice.{meta['appservice_id']}.users]]",
        "exclusive = true",
        f"regex = {quoted(namespace)}",
    ]) + "\n"


def validate_meta(meta):
    require(meta.get("format") == FORMAT, "Unsupported state format")
    require(meta.get("mode") in MODES, "Unknown mode")
    require(isinstance(meta.get("server_name"), str) and SERVER_NAME.fullmatch(meta["server_name"]),
            "server_name must be a lowercase hostname with an optional :port")
    _, named_port = split_name(meta["server_name"])
    require(named_port is None or 1 <= named_port <= 65535, "server_name port out of range")
    require(isinstance(meta.get("port"), int) and 1 <= meta["port"] <= 65535, "Port out of range")
    address = ipaddress.ip_address(meta.get("address", ""))
    if meta["mode"] in ("local", "proxied"):
        require(address.is_loopback, f"{meta['mode'].capitalize()} mode listens on loopback only")
    if meta["mode"] == "proxied":
        public = meta.get("public_host")
        require(isinstance(public, str) and SERVER_NAME.fullmatch(public) and ":" not in public,
                "public_host must be a lowercase hostname without a port")
        for peer in meta.get("peer_networks", []):
            require(inside_peer_range(peer), f"peer network {peer} is not inside a private range")
    else:
        require(not meta.get("public_host") and not meta.get("peer_networks"),
                "public_host and peer_networks apply to proxied mode only")
    require(LOCALPART.fullmatch(meta.get("sender_localpart", "")), "sender_localpart must be lowercase [a-z0-9_]")
    require(LOCALPART.fullmatch(meta.get("namespace_prefix", "")), "namespace_prefix must be lowercase [a-z0-9_]")
    require(re.fullmatch(r"[a-z][a-z0-9_]*", meta.get("appservice_id", "")), "appservice_id must be a lowercase identifier")
    for name in meta.get("trusted_roots", []):
        require(re.fullmatch(r"[A-Za-z0-9._-]+\.(pem|crt)", name), "trusted root file names must be simple .pem or .crt names")
    if "identity_provider" in meta:
        validate_idp(meta)
    return meta


def validate_idp(meta):
    idp = meta["identity_provider"]
    require(meta["mode"] == "proxied", "An identity provider needs proxied mode, which has a public HTTPS host")
    require(isinstance(idp, dict), "identity_provider must be an object")
    require(isinstance(idp.get("client_id"), str) and CLIENT_ID.fullmatch(idp["client_id"]),
            "client_id must be 1-128 characters of [A-Za-z0-9._~-]")
    issuer = idp.get("issuer_url")
    require(isinstance(issuer, str) and re.fullmatch(r"https://[a-z0-9.-]+(:[0-9]{1,5})?(/[^\s\"\\]*)?", issuer),
            "issuer_url must be an https URL on a lowercase host")
    for field in ("brand", "name"):
        require(isinstance(idp.get(field), str) and re.fullmatch(r"[A-Za-z0-9 ._-]{1,64}", idp[field]),
                f"{field} must be short and plain")
    require(isinstance(idp.get("registration"), bool), "registration must be true or false")
    secret = idp.get("client_secret_file")
    require(isinstance(secret, str) and Path(secret).is_absolute() and "\"" not in secret and "\\" not in secret,
            "client_secret_file must be an absolute path")


def callback_url(meta):
    idp = meta["identity_provider"]
    return f"https://{meta['public_host']}/_matrix/client/unstable/login/sso/callback/{quote(idp['client_id'], safe='')}"


def identity_provider_text(meta):
    """One [[global.identity_provider]] for a private server: accounts are named
    from preferred_username only, never invented, and never matched onto an
    existing account."""
    idp = meta["identity_provider"]
    return "\n".join([
        "# Written by bundle.py idp. The secret is read from client_secret_file at startup.",
        "# client_id and issuer_url must never change once anyone has signed in:",
        "# tuwunel keys each stored identity to them.",
        "[[global.identity_provider]]",
        f"brand = {quoted(idp['brand'])}",
        f"name = {quoted(idp['name'])}",
        f"client_id = {quoted(idp['client_id'])}",
        f"issuer_url = {quoted(idp['issuer_url'])}",
        f"callback_url = {quoted(callback_url(meta))}",
        f"client_secret_file = {quoted(idp['client_secret_file'])}",
        'userid_claims = ["preferred_username"]',
        "unique_id_fallbacks = false",
        "trusted = false",
        f"registration = {'true' if idp['registration'] else 'false'}",
        "default = true",
    ]) + "\n"


def check_secret_file(path):
    """The file must be private, a regular file, and not empty. Its contents
    are read only to test that, and never shown."""
    require(path.is_file() and not path.is_symlink(), f"{path} must be a regular file")
    require(path.stat().st_mode & 0o077 == 0, f"{path} must not be readable by group or others (chmod 600)")
    require(path.read_text(encoding="utf-8").strip(), f"{path} is empty")


def idp(args):
    """Adds or updates the deployment's identity provider. The client ID and
    issuer are permanent once set; only the other fields may change.

    The provider file is replaced before bundle.json, so an interrupted run
    leaves the two disagreeing. load() refuses that state everywhere but here,
    and rerunning idp is the repair."""
    root, meta, binary = load(args.root, idp_agreement=False)
    require(meta["mode"] == "proxied", "An identity provider needs proxied mode, which has a public HTTPS host")
    secret = Path(os.path.abspath(Path(args.secret_file).expanduser())) if args.secret_file \
        else root / "config" / "oidc-client-secret"
    check_secret_file(secret)
    previous = meta.get("identity_provider")
    if previous:
        require(previous["client_id"] == args.client_id and previous["issuer_url"] == args.issuer_url,
                "client_id and issuer_url cannot change once set; tuwunel keys stored identities to them")
    meta["identity_provider"] = {"brand": args.brand, "name": args.name or args.brand,
                                 "client_id": args.client_id, "issuer_url": args.issuer_url,
                                 "client_secret_file": str(secret), "registration": not args.no_registration}
    validate_meta(meta)
    target = root / "config" / IDP_FILE
    staged = target.with_suffix(".toml.new")
    staged.touch(mode=0o600)
    staged.write_text(identity_provider_text(meta))
    os.replace(staged, target)
    staged_meta = root / "bundle.json.new"
    staged_meta.touch(mode=0o600)
    staged_meta.write_text(json.dumps(meta, indent=2) + "\n")
    os.replace(staged_meta, root / "bundle.json")
    print(f"redirect URI to register: {callback_url(meta)}")
    print(" ".join(command(root, binary)))


# A test CA is a directory: ca.key (0600), ca.crt, ca.json. The key never
# leaves it; instances only receive certificates issued from it and the
# public ca.crt. Everything it produces is marked test-only; a real deployment
# brings certificates from an operator's authority instead.
CA_DAYS, CERT_DAYS, EXPIRY_WARN_DAYS = 90, 30, 7


def ca_init(cadir, name):
    cadir = safe_root(cadir)
    require(not cadir.exists(), "CA directory already exists; choose a new one")
    require(re.fullmatch(r"[A-Za-z0-9 ._-]{1,64}", name), "CA name must be short and plain")
    cadir.mkdir(mode=0o700)
    run("openssl", "req", "-x509", "-newkey", "rsa:3072", "-noenc", "-sha256", "-days", str(CA_DAYS),
        "-subj", f"/CN={name}", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout", str(cadir / "ca.key"), "-out", str(cadir / "ca.crt"))
    (cadir / "ca.key").chmod(0o600)
    meta = {"format": FORMAT, "name": name, "test_only": True,
            "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (cadir / "ca.json").write_text(json.dumps(meta, indent=2) + "\n")
    return cadir


def ca_load(cadir):
    cadir = safe_root(cadir)
    for name in ("ca.key", "ca.crt", "ca.json"):
        require((cadir / name).is_file(), f"Not a CA directory: missing {name}")
    meta = json.loads((cadir / "ca.json").read_text())
    require(meta.get("format") == FORMAT and meta.get("test_only") is True, "Unsupported CA metadata")
    return cadir


def ca_issue(cadir, host, outdir, days=CERT_DAYS):
    """Issues server.key and server.crt for `host` into `outdir`, replacing any there.

    Both files are produced under temporary names, checked against each
    other, and renamed into place only then, so an interruption never leaves
    a new key beside the old certificate. The two renames are not one atomic
    step; a reload that lands between them fails and keeps the previous pair,
    which is why `renew` signals only after this returns.
    """
    cadir = ca_load(cadir)
    require(SERVER_NAME.fullmatch(host) and ":" not in host, "host must be a bare lowercase hostname")
    key, crt = outdir / "server.key", outdir / "server.crt"
    new_key, new_crt = outdir / ".server.key.new", outdir / ".server.crt.new"
    csr, ext = outdir / f".{host}.csr", outdir / f".{host}.ext"
    try:
        run("openssl", "req", "-new", "-newkey", "rsa:3072", "-noenc", "-sha256", "-subj", f"/CN={host}",
            "-keyout", str(new_key), "-out", str(csr))
        new_key.chmod(0o600)
        ext.write_text(f"subjectAltName=DNS:{host},IP:127.0.0.1\nbasicConstraints=critical,CA:FALSE\n"
                       "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
        run("openssl", "x509", "-req", "-in", str(csr), "-CA", str(cadir / "ca.crt"), "-CAkey", str(cadir / "ca.key"),
            "-set_serial", str(secrets.randbits(128)), "-days", str(days), "-sha256", "-extfile", str(ext),
            "-out", str(new_crt))
        new_crt.chmod(0o600)
        require(run("openssl", "pkey", "-in", str(new_key), "-pubout") == run("openssl", "x509", "-in", str(new_crt), "-noout", "-pubkey"),
                "The issued certificate does not match its key")
        os.replace(new_crt, crt)
        os.replace(new_key, key)
    finally:
        for leftover in (csr, ext, new_key, new_crt):
            leftover.unlink(missing_ok=True)
    return crt


def cert_days_left(path):
    text = run("openssl", "x509", "-in", str(path), "-noout", "-enddate")
    return days_until(text.strip().partition("=")[2])


def days_until(not_after, now=None):
    """Days from NOW until an OpenSSL notAfter string such as
    'Jan  2 03:04:05 2027 GMT'. OpenSSL prints GMT, so the tuple is read as
    UTC; mktime would apply the host's zone and shift the answer by its
    offset."""
    expires = time.strptime(not_after, "%b %d %H:%M:%S %Y %Z")
    return (calendar.timegm(expires) - (time.time() if now is None else now)) / 86400


def cert_serial(path):
    return run("openssl", "x509", "-in", str(path), "-noout", "-serial").strip().partition("=")[2]


def ca_command(args):
    if args.ca_verb == "init":
        print(ca_init(args.cadir, args.name))
    elif args.ca_verb == "issue":
        out = safe_root(args.out)
        require(out.is_dir(), "--out must be an existing directory")
        print(ca_issue(args.cadir, args.host, out, args.days))
    elif args.ca_verb == "export":
        cadir = ca_load(args.cadir)
        target = Path(os.path.abspath(args.out)) if args.out else None
        if target:
            shutil.copyfile(cadir / "ca.crt", target)
            print(target)
        else:
            sys.stdout.write((cadir / "ca.crt").read_text())
    elif args.ca_verb == "status":
        cadir = ca_load(args.cadir)
        print(f"{cadir / 'ca.crt'}: {cert_days_left(cadir / 'ca.crt'):.1f} days left")
        for cert in args.cert:
            print(f"{cert}: {cert_days_left(Path(cert)):.1f} days left")


def run(*args):
    # Never pass credentials as arguments or relay output on error.
    result = subprocess.run(args, capture_output=True, text=True)
    require(result.returncode == 0, f"{args[0]} failed; rerun it separately")
    return result.stdout


def init(args):
    root = safe_root(args.root)
    require(not root.exists(), "Destination already exists; choose a new directory")
    binary = Path(os.path.abspath(args.binary))
    require(binary.is_file() and os.access(binary, os.X_OK), "--binary must be an executable file")
    profile = PROFILES / f"{args.mode}.toml"
    require(profile.is_file(), "Profile file missing")
    address = args.address or ("0.0.0.0" if args.mode == "federated" else "127.0.0.1")
    if args.mode == "proxied":
        require(args.public_host, "Proxied mode needs --public-host, the name the proxy serves")
    else:
        require(not args.public_host and not args.peer_network,
                "--public-host and --peer-network apply to proxied mode only")
    trust = [Path(os.path.abspath(p)) for p in args.trust]
    require(all(p.is_file() for p in trust), "--trust files must exist")
    require(len({p.name for p in trust}) == len(trust), "--trust file names must be distinct")
    meta = validate_meta({"format": FORMAT, "mode": args.mode, "server_name": args.server_name,
                          "address": address, "port": args.port, "appservice_id": "terva",
                          "sender_localpart": args.sender_localpart, "namespace_prefix": args.namespace_prefix,
                          "trusted_roots": [p.name for p in trust],
                          "fixture": bool(args.fixture),
                          **({"public_host": args.public_host,
                              "peer_networks": [str(ipaddress.ip_network(p)) for p in args.peer_network]}
                             if args.mode == "proxied" else {})})
    sources = sum(1 for flag in (args.test_ca, args.ca, args.tls_cert or args.tls_key) if flag)
    if args.mode == "federated":
        require(sources == 1, "Federated mode needs exactly one of --test-ca, --ca, or --tls-cert with --tls-key")
        if args.tls_cert or args.tls_key:
            require(args.tls_cert and args.tls_key, "--tls-cert and --tls-key go together")
            require(Path(args.tls_cert).is_file() and Path(args.tls_key).is_file(), "Certificate files must exist")
        if args.ca:
            ca_load(args.ca)
    else:
        require(sources == 0, "TLS options apply to federated mode only")
        require(not args.fixture, "--fixture applies to federated mode only")

    version = run(str(binary), "--version").strip()
    root.mkdir(mode=0o700)
    for name in ("config", "data", "run") + (("tls",) if args.mode == "federated" else ()):
        (root / name).mkdir(mode=0o700)
    if trust:
        (root / "tls").mkdir(mode=0o700, exist_ok=True)
        (root / "tls" / "trusted").mkdir(mode=0o700)
        for source in trust:
            shutil.copyfile(source, root / "tls" / "trusted" / source.name)
    shutil.copyfile(profile, root / "config" / "profile.toml")
    (root / "config" / "tuwunel.toml").write_text(deployment_text(meta, root))
    as_token, hs_token = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    secret = root / "config" / "appservice.toml"
    secret.touch(mode=0o600)
    secret.write_text(appservice_text(meta, as_token, hs_token))
    ca = None
    if args.mode == "federated":
        host, _ = split_name(meta["server_name"])
        if args.test_ca:
            ca = ca_init(root / "ca-private", f"Terva bundle test CA for {host}")
        elif args.ca:
            ca = ca_load(args.ca)
        if ca:
            ca_issue(ca, host, root / "tls")
            shutil.copyfile(ca / "ca.crt", root / "tls" / "ca.crt")
        else:
            shutil.copyfile(args.tls_cert, root / "tls" / "server.crt")
            shutil.copyfile(args.tls_key, root / "tls" / "server.key")
    run(str(binary), f"--generate-config={root / 'config' / 'tuwunel-example.toml'}")
    for file in root.rglob("*"):
        if file.is_file():
            file.chmod(0o600)
    meta.update({"binary": str(binary), "binary_sha256": digest(binary), "version": version,
                 "test_ca": ca is not None, "ca": str(ca) if ca else None,
                 "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    with (root / "bundle.json").open("x", encoding="utf-8") as stream:
        json.dump(meta, stream, indent=2)
        stream.write("\n")
    (root / "bundle.json").chmod(0o600)
    print(" ".join(command(root, binary)))


def load(root, idp_agreement=True):
    root = safe_root(root)
    meta = validate_meta(json.loads((root / "bundle.json").read_text()))
    binary = Path(meta["binary"])
    require(binary.is_file(), "Recorded binary is missing")
    require(digest(binary) == meta["binary_sha256"],
            "Binary changed since init; re-run init or record the upgrade deliberately")
    for path in config_files(root):
        require(path.is_file(), f"Missing {path.name}")
    require(not idp_agreement or ("identity_provider" in meta) == (root / "config" / IDP_FILE).is_file(),
            f"bundle.json and config/{IDP_FILE} disagree about the identity provider; rerun idp to repair")
    return root, meta, binary


def renew(args):
    """Reissues the server certificate from the recorded CA and, with --pid,
    asks the running server to reload it."""
    root, meta, _ = load(args.root)
    require(meta["mode"] == "federated" and meta.get("ca"), "renew needs a federated deployment issued from a CA")
    before = cert_serial(root / "tls" / "server.crt")
    host, _ = split_name(meta["server_name"])
    ca_issue(Path(meta["ca"]), host, root / "tls", args.days)
    after = cert_serial(root / "tls" / "server.crt")
    require(before != after, "Reissue produced the same serial")
    if args.pid:
        os.kill(args.pid, signal.SIGUSR1)
    print(f"reissued {root / 'tls' / 'server.crt'}: serial {before} -> {after}"
          + (f", SIGUSR1 sent to {args.pid}" if args.pid else ""))


def show_command(args):
    root, _, binary = load(args.root)
    print(" ".join(command(root, binary)))


class Check:
    def __init__(self, root, meta, binary):
        self.root, self.meta, self.binary = root, meta, binary
        self.process = None
        self.results = []
        self.log = (root / "run" / "check.log").open("a", encoding="utf-8")
        with (root / "config" / "appservice.toml").open("rb") as stream:
            registration = tomllib.load(stream)["global"]["appservice"][meta["appservice_id"]]
        self.token = registration["as_token"]
        host, _ = split_name(meta["server_name"])
        self.host = host
        if meta["mode"] == "federated":
            cafile = str(root / "tls" / "ca.crt") if meta["test_ca"] else None
            self.context = ssl.create_default_context(cafile=cafile)
        else:
            self.context = None

    def record(self, name, condition=True, **details):
        self.results.append({"name": name, "ok": bool(condition), **details})
        print(("ok   " if condition else "FAIL ") + name)
        return condition

    def start(self):
        self.log.write(f"--- start {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        self.log.flush()
        self.process = subprocess.Popen(command(self.root, self.binary), stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + HEALTH_WAIT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                return self.record("start", False, exit=self.process.returncode)
            probe = subprocess.run(command(self.root, self.binary) + ["--health-check"],
                                   stdin=subprocess.DEVNULL, capture_output=True)
            if probe.returncode == 0:
                return self.record("start", True, health_after=round(HEALTH_WAIT - (deadline - time.monotonic()), 1))
            time.sleep(1)
        return self.record("start", False, reason="health check never passed")

    def stop(self):
        if self.process is None or self.process.poll() is not None:
            return
        began = time.monotonic()
        self.process.send_signal(signal.SIGTERM)
        try:
            code = self.process.wait(STOP_GRACE)
            self.record("stop", code == 0, exit=code, seconds=round(time.monotonic() - began, 1))
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            self.record("stop", False, reason=f"still running after {STOP_GRACE}s, killed")

    def api(self, method, path, body=None, *, user=None, device=None, auth=True, token=None):
        query = {}
        if user:
            query["user_id"] = user
        if device:
            query["device_id"] = device
        if query:
            path += ("&" if "?" in path else "?") + urlencode(query)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {token or self.token}"
        if self.context:
            # Connect to loopback but verify the certificate against the server
            # host, which is the name the certificate carries.
            raw = socket.create_connection(("127.0.0.1", self.meta["port"]), timeout=30)
            conn = http.client.HTTPSConnection(self.host, self.meta["port"], timeout=30, context=self.context)
            conn.sock = self.context.wrap_socket(raw, server_hostname=self.host)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.meta["port"], timeout=30)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = {"raw": payload[:200].decode("utf-8", "replace")}
        return response.status, data

    def exercise(self):
        server = self.meta["server_name"]
        agent = f"{self.meta['namespace_prefix']}check_{secrets.token_hex(3)}"
        user, device = f"@{agent}:{server}", "BUNDLECHECK1"
        status, data = self.api("GET", "/_matrix/client/versions", auth=False)
        self.record("versions", status == 200 and "versions" in data, status=status)
        status, data = self.api("POST", f"{PREFIX}/register",
                                {"type": "m.login.application_service", "username": agent, "inhibit_login": True})
        self.record("register", status == 200 and data.get("user_id") == user, status=status)
        status, _ = self.api("PUT", f"{PREFIX}/devices/{quote(device)}", {"display_name": "bundle check"}, user=user)
        self.record("put-device", status in (200, 201), status=status)
        status, data = self.api("GET", f"{PREFIX}/account/whoami", user=user, device=device)
        self.record("whoami", status == 200 and data.get("device_id") == device, status=status)
        status, data = self.api("POST", f"{PREFIX}/createRoom",
                                {"visibility": "private", "preset": "private_chat", "name": "bundle check",
                                 "initial_state": []}, user=user, device=device)
        room = data.get("room_id", "")
        self.record("create-room", status == 200 and room.startswith("!"), status=status)
        encoded = quote(room, safe="")
        status, data = self.api("GET", f"{PREFIX}/rooms/{encoded}/state/m.room.encryption/", user=user, device=device)
        self.record("plaintext-room", status == 404 and data.get("errcode") == "M_NOT_FOUND", status=status)
        status, data = self.api("PUT", f"{PREFIX}/rooms/{encoded}/send/m.room.message/{secrets.token_hex(8)}",
                                {"msgtype": "m.text", "body": "bundle check, synthetic content only"},
                                user=user, device=device)
        event = data.get("event_id", "")
        self.record("send", status == 200 and event.startswith("$"), status=status)
        status, data = self.api("GET", f"{PREFIX}/sync?timeout=0&set_presence=offline", user=user, device=device)
        seen = any(e.get("event_id") == event
                   for e in data.get("rooms", {}).get("join", {}).get(room, {}).get("timeline", {}).get("events", []))
        self.record("sync", status == 200 and seen, status=status)
        return user, device, room

    def persisted(self, user, device, room):
        status, data = self.api("GET", f"{PREFIX}/devices", user=user, device=device)
        devices = [d.get("device_id") for d in data.get("devices", [])]
        self.record("device-survives-restart", status == 200 and device in devices, status=status)
        status, data = self.api("GET", f"{PREFIX}/joined_rooms", user=user, device=device)
        self.record("room-survives-restart", status == 200 and room in data.get("joined_rooms", []), status=status)
        if self.meta["mode"] == "federated":
            status, data = self.api("GET", "/.well-known/matrix/server", auth=False)
            self.record("well-known-server", status == 200 and "m.server" in data, status=status)
        elif self.meta["mode"] == "proxied":
            status, data = self.api("GET", "/.well-known/matrix/server", auth=False)
            self.record("well-known-server", status == 200 and data.get("m.server") == self.meta["public_host"] + ":443",
                        status=status)
            status, data = self.api("GET", "/_matrix/federation/v1/version", auth=False)
            self.record("federation-answers", status == 200 and "server" in data, status=status)
        else:
            status, data = self.api("GET", "/_matrix/federation/v1/version", auth=False)
            self.record("federation-refused", status in (403, 404), status=status)

    def expiry(self):
        if self.meta["mode"] != "federated":
            return
        for name in ("server.crt", "ca.crt"):
            path = self.root / "tls" / name
            if path.is_file():
                days = cert_days_left(path)
                self.record(f"expiry-{name}", days > EXPIRY_WARN_DAYS, days_left=round(days, 1))

    def report(self):
        ok = all(r["ok"] for r in self.results)
        summary = {"ok": ok, "mode": self.meta["mode"], "version": self.meta["version"],
                   "binary_sha256": self.meta["binary_sha256"],
                   "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "results": self.results}
        (self.root / "run" / "check.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(("PASS" if ok else "FAIL") + f": {sum(r['ok'] for r in self.results)}/{len(self.results)}, "
              f"report in {self.root / 'run' / 'check.json'}, server log in {self.root / 'run' / 'check.log'}")
        return ok


def check(args):
    root, meta, binary = load(args.root)
    checker = Check(root, meta, binary)
    try:
        checker.expiry()
        if checker.start():
            user, device, room = checker.exercise()
            checker.stop()
            if checker.start():
                checker.persisted(user, device, room)
        checker.stop()
    finally:
        if checker.process and checker.process.poll() is None:
            checker.process.kill()
            checker.process.wait()
            checker.record("cleanup", False, reason="server killed after an error")
    return 0 if checker.report() else 1


PROBE_MAX_BYTES = 1 << 20
PROBE_DEADLINE = 30


def fetch(url, context):
    """GET over verified HTTPS and parse JSON. Returns (status, data). A body
    over PROBE_MAX_BYTES, or one still arriving after PROBE_DEADLINE seconds,
    raises HTTPException: the socket timeout alone does not stop a trickle."""
    _, _, host, path = (url.split("/", 3) + [""])[:4]
    deadline = time.monotonic() + PROBE_DEADLINE
    conn = http.client.HTTPSConnection(host, timeout=15, context=context)
    try:
        conn.request("GET", "/" + path)
        response = conn.getresponse()
        chunks, size = [], 0
        while chunk := response.read1(65536):
            size += len(chunk)
            if size > PROBE_MAX_BYTES:
                raise http.client.HTTPException(f"response over {PROBE_MAX_BYTES} bytes")
            if time.monotonic() > deadline:
                raise http.client.HTTPException(f"response still arriving after {PROBE_DEADLINE} s")
            chunks.append(chunk)
        payload = b"".join(chunks)
    finally:
        conn.close()
    try:
        return response.status, (json.loads(payload) if payload else {})
    except (json.JSONDecodeError, UnicodeDecodeError):
        return response.status, {}


def dig(data, *keys):
    """The value under nested object keys, or None where any level is missing
    or is not an object. A server's JSON is not trusted to have our shape."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def text(value):
    return value if isinstance(value, str) else ""


def usable_ed25519_keys(verify_keys):
    """The ed25519 key IDs whose key is unpadded base64 of 32 bytes."""
    usable = []
    for key_id, entry in (verify_keys.items() if isinstance(verify_keys, dict) else []):
        key = text(dig(entry, "key"))
        if not key_id.startswith("ed25519:") or not key or "=" in key:
            continue
        try:
            raw = base64.b64decode(key + "=" * (-len(key) % 4), validate=True)
        except ValueError:
            continue
        if len(raw) == 32:
            usable.append(key_id)
    return usable


def probe(args):
    """A reachability check of the discovery path a peer follows, against a
    running server, over the real names and the system trust store. It does not
    follow SRV records or verify signatures; bundled-profile.md says why."""
    name = args.server_name
    require(SERVER_NAME.fullmatch(name), "server_name must be a lowercase hostname with an optional :port")
    context = ssl.create_default_context(cafile=args.cafile)
    results = []

    def record(check, condition, **details):
        results.append({"name": check, "ok": bool(condition), **details})
        print(("ok   " if condition else "FAIL ") + check + (f" {details}" if details else ""))

    def get(check, url):
        try:
            return fetch(url, context)
        except (OSError, ssl.SSLError, http.client.HTTPException, ValueError) as error:
            # ValueError: a delegated endpoint that is not a valid host and port.
            record(check, False, error=type(error).__name__)
            return None, {}

    # Server discovery as the spec orders it: a name with an explicit port is
    # used as it stands, and only a bare name is delegated. Both well-known
    # documents are fetched from the hostname on 443, never from the port.
    host, port = split_name(name)
    base = f"https://{host}"
    delegated = ""
    if not port:
        status, data = get("well-known-server", f"{base}/.well-known/matrix/server")
        delegated = text(dig(data, "m.server")) if status == 200 else ""
        if status is not None:
            record("well-known-server", bool(SERVER_NAME.fullmatch(delegated)), status=status, server=delegated)
        if not SERVER_NAME.fullmatch(delegated):
            delegated = ""
    status, data = get("well-known-client", f"{base}/.well-known/matrix/client")
    client = text(dig(data, "m.homeserver", "base_url")).rstrip("/") if status == 200 else ""
    if status is not None:
        record("well-known-client", client.startswith("https://"), status=status, base_url=client)
    # A delegation or name without a port goes to 8448. A peer would try SRV
    # records first; the standard library cannot resolve them, so a server
    # reached only through SRV fails here while a peer could still find it.
    target = f"{host}:{port}" if port else delegated or host
    endpoint = "https://" + (target if split_name(target)[1] else f"{target}:8448")
    status, data = get("client-versions", f"{client or endpoint}/_matrix/client/versions")
    if status is not None:
        record("client-versions", status == 200 and isinstance(dig(data, "versions"), list), status=status)
    status, data = get("federation-version", f"{endpoint}/_matrix/federation/v1/version")
    if status is not None:
        record("federation-version", status == 200 and isinstance(dig(data, "server"), dict), status=status,
               version=dig(data, "server", "version"))
    status, data = get("server-key", f"{endpoint}/_matrix/key/v2/server")
    if status is not None:
        keys = usable_ed25519_keys(dig(data, "verify_keys"))
        signed = [k for k in keys if text(dig(data, "signatures", name, k))]
        valid_until = dig(data, "valid_until_ts")
        current = isinstance(valid_until, int) and valid_until > time.time() * 1000
        record("server-key", status == 200 and dig(data, "server_name") == name and bool(signed) and current,
               status=status, server_name=dig(data, "server_name"), signed_by=signed, current=current)
    ok = all(r["ok"] for r in results)
    print(("PASS" if ok else "FAIL") + f": {sum(r['ok'] for r in results)}/{len(results)} for {name}")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("init", help="create a state directory")
    p.add_argument("root")
    p.add_argument("--binary", required=True)
    p.add_argument("--server-name", required=True, help="cannot change after first boot; no default")
    p.add_argument("--mode", choices=MODES, default="local")
    p.add_argument("--port", type=int, default=8008)
    p.add_argument("--address", help="listener address; local and proxied modes allow loopback only")
    p.add_argument("--public-host", help="proxied mode: the HTTPS host the proxy serves on 443")
    p.add_argument("--peer-network", action="append", default=[], metavar="CIDR",
                   help="proxied mode: an internal network other homeservers federate from; repeatable")
    p.add_argument("--sender-localpart", default="terva_service")
    p.add_argument("--namespace-prefix", default="terva_")
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    p.add_argument("--test-ca", action="store_true", help="generate a private CA for an isolated check")
    p.add_argument("--ca", metavar="CADIR", help="issue the server certificate from this test CA directory")
    p.add_argument("--fixture", action="store_true",
                   help="TEST ONLY: loopback federation and short retry timings for checks on one machine")
    p.add_argument("--trust", action="append", default=[], metavar="PEM",
                   help="a root certificate to trust for outbound HTTPS, in addition to the platform store; repeatable")
    p.set_defaults(func=init)
    p = sub.add_parser("ca", help="manage a test-only certificate authority directory")
    ca_sub = p.add_subparsers(dest="ca_verb", required=True)
    q = ca_sub.add_parser("init", help="create a CA directory"); q.add_argument("cadir"); q.add_argument("--name", required=True)
    q = ca_sub.add_parser("issue", help="issue a server certificate into a directory")
    q.add_argument("cadir"); q.add_argument("--host", required=True); q.add_argument("--out", required=True)
    q.add_argument("--days", type=int, default=CERT_DAYS)
    q = ca_sub.add_parser("export", help="write the root certificate, to stdout or --out"); q.add_argument("cadir"); q.add_argument("--out")
    q = ca_sub.add_parser("status", help="days left on the root and any given certificates")
    q.add_argument("cadir"); q.add_argument("cert", nargs="*")
    p.set_defaults(func=ca_command)
    p = sub.add_parser("renew", help="reissue a deployment's certificate from its CA; --pid sends SIGUSR1")
    p.add_argument("root"); p.add_argument("--pid", type=int); p.add_argument("--days", type=int, default=CERT_DAYS)
    p.set_defaults(func=renew)
    p = sub.add_parser("idp", help="proxied mode: sign humans in through an OpenID Connect provider")
    p.add_argument("root")
    p.add_argument("--issuer-url", required=True, help="exactly the provider's issuer; permanent")
    p.add_argument("--client-id", required=True, help="the client ID registered with the provider; permanent")
    p.add_argument("--brand", default="Authentik")
    p.add_argument("--name", help="shown on the login page; defaults to the brand")
    p.add_argument("--secret-file", help="0600 file holding the client secret (default ROOT/config/oidc-client-secret)")
    p.add_argument("--no-registration", action="store_true",
                   help="admit only accounts that have signed in through this provider before; "
                        "an account made any other way is refused")
    p.set_defaults(func=idp)
    p = sub.add_parser("command", help="print the supervisor command line")
    p.add_argument("root")
    p.set_defaults(func=show_command)
    p = sub.add_parser("check", help="start, exercise, restart, verify persistence, stop")
    p.add_argument("root")
    p.set_defaults(func=check)
    p = sub.add_parser("probe", help="check that a running server's discovery path answers; not a full peer")
    p.add_argument("server_name")
    p.add_argument("--cafile", help="trust this CA instead of the system store, for a test authority")
    p.set_defaults(func=probe)
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
