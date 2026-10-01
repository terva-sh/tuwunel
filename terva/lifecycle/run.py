#!/usr/bin/env python3
"""Two-server Matrix lifecycle checks. All generated artifacts stay outside Git."""

import argparse
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import signal
import subprocess
import sys
import time
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "development"))
import dev

LABEL = "sh.terva.lifecycle.run"
PREFIX = "/_matrix/client/v3"


def command(*args, timeout=180):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    dev.require(result.returncode == 0, f"Command failed: {args[0]} {args[1]}")
    return result.stdout


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def deny_except(addresses):
    """Deny the whole address space except these exact IPv4 hosts."""
    networks = [ipaddress.ip_network("0.0.0.0/0")]
    for address in addresses:
        host = ipaddress.ip_network(address + "/32")
        networks = [part for network in networks for part in
                    (network.address_exclude(host) if host.subnet_of(network) else [network])]
    return [str(n) for n in networks] + ["::/0"]


def seal(root, meta):
    meta["assets"] = {name: dev.digest(root / name) for name in ("compose.yaml", "credentials.json")}
    for folder in ("config", "tls"):
        meta["assets"].update({f"{folder}/{k}": v for k, v in dev.inventory(root / folder).items()})
    save(root / "instance.json", meta)


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(check, seconds, description):
    deadline = time.monotonic() + seconds
    while True:
        try:
            if check():
                return
        except (TimeoutError, ConnectionError, http.client.RemoteDisconnected):
            # Recovery can interrupt a read. Keep the last confirmed sync cursor.
            pass
        dev.require(time.monotonic() < deadline, f"Deadline exceeded: {description}; not proof of permanent loss")
        time.sleep(0.5)


def owned(item, run_id):
    labels = item.get("Config", {}).get("Labels", item.get("Labels", {})) or {}
    dev.require(labels.get(LABEL) == run_id, "Refusing to change a resource owned by another run")


def cleanup(root):
    state = json.loads((root / "owner.json").read_text())
    run_id = state["run_id"]
    dev.require(re.fullmatch(r"tuwunel-life-[0-9a-f]{12}", run_id), "Invalid run owner")
    # Enumerate by the random run label, then verify names before using immutable IDs.
    for kind, names in (("container", state["containers"]), ("network", state["networks"])):
        args = ["docker", kind, "ls", "--filter", f"label={LABEL}={run_id}", "--format", "{{.ID}}"]
        if kind == "container":
            args.insert(3, "-a")
        for resource_id in command(*args).split():
            item = json.loads(command("docker", kind, "inspect", resource_id))[0]
            owned(item, run_id)
            name = item["Name"].lstrip("/")
            dev.require(name in names, "Unexpected resource carries this run label; refusing cleanup")
            if kind == "container":
                command("docker", "container", "stop", "--time", "15", resource_id, timeout=30)
            command("docker", kind, "rm", resource_id)
        dev.require(not command(*args).strip(), f"Owned {kind} resources remain after cleanup")


class Fixture:
    def __init__(self, root, deadline):
        self.root = root
        self.deadline = deadline
        self.nodes = []
        self.report = {"format": 1, "started": datetime.now(timezone.utc).isoformat(), "assertions": [], "status": "running"}
        self.state = {"run_id": "tuwunel-life-" + secrets.token_hex(6), "containers": [], "networks": []}

    def record(self, name, condition=True, **details):
        self.report["assertions"].append({"name": name, "passed": bool(condition), **details})
        save(self.root / "results.json", self.report)
        dev.require(condition, f"Assertion failed: {name}")
        print("PASS " + name, flush=True)

    def setup(self, image, relay_image):
        dev.new_directory(self.root)
        save(self.root / "owner.json", self.state)
        run_id = self.state["run_id"]
        network = run_id + "-peers"
        self.state["networks"].append(network)
        save(self.root / "owner.json", self.state)
        command("docker", "network", "create", "--internal", "--label", f"{LABEL}={run_id}", network)
        info = json.loads(command("docker", "network", "inspect", network))[0]
        subnet = ipaddress.ip_network(info["IPAM"]["Config"][0]["Subnet"])
        dev.require(subnet.version == 4 and subnet.num_addresses >= 32, "Need an IPv4 Docker subnet with room for two peers")
        addresses = [str(subnet.network_address + 10), str(subnet.network_address + 11)]
        ports = set()
        for index, letter in enumerate(("a", "b")):
            node_root = self.root / letter
            port = available_port()
            while port in ports:
                port = available_port()
            ports.add(port)
            host = letter + "-" + run_id.removeprefix("tuwunel-life-") + ".test"
            dev.init(node_root, image, relay_image, host, port)
            meta = dev.load(node_root)
            self.nodes.append({"root": node_root, "meta": meta, "address": addresses[index]})
            identifier = dev.identity(meta)
            self.state["containers"].extend([identifier, identifier + "-relay"])
            self.state["networks"].extend([identifier + "-net", identifier + "-loopback"])
            save(self.root / "owner.json", self.state)
        bundle = b"".join((node["root"] / "tls/ca.crt").read_bytes() for node in self.nodes)
        for node in self.nodes:
            root, meta = node["root"], node["meta"]
            (root / "tls/peers.pem").write_bytes(bundle)
            config = root / "config/tuwunel.toml"
            config.write_text(config.read_text().replace("[global]\n", "[global]\n" +
                "ip_range_denylist = " + json.dumps(deny_except(addresses)) + "\n" +
                "request_conn_timeout = 2\nfederation_timeout = 3\nsender_timeout = 5\nsender_retry_grace = 2\n"))
            plan = json.loads(dev.compose(root, meta, "config", "--format", "json"))
            for service in plan["services"].values():
                service.setdefault("labels", {})[LABEL] = run_id
            for net in plan["networks"].values():
                net.setdefault("labels", {})[LABEL] = run_id
            plan["networks"]["peers"] = {"external": True, "name": network}
            server = plan["services"]["homeserver"]
            server["networks"]["peers"] = {"ipv4_address": node["address"], "aliases": [meta["host"]]}
            server["environment"]["SSL_CERT_FILE"] = "/tls/peers.pem"
            # JSON is valid YAML and avoids another runtime package dependency.
            save(root / "compose.yaml", plan)
            seal(root, meta)
        self.bad_certificate()
        self.report["versions"] = {"docker": command("docker", "version", "--format", "{{.Server.Version}}").strip(),
            "compose": command("docker", "compose", "version").strip(), "python": sys.version.split()[0],
            "openssl": command("openssl", "version").strip(), "checkout_commit": command("git", "rev-parse", "HEAD").strip(),
            "checkout_dirty": bool(command("git", "status", "--porcelain").strip()),
            "runner_sha256": dev.digest(Path(__file__)), "development_helper_sha256": dev.digest(Path(dev.__file__)),
            "server_source_tag": image, "image": self.nodes[0]["meta"]["image"],
            "relay_image": self.nodes[0]["meta"]["relay_image"]}
        self.report["run_id"] = run_id
        self.report["peer_addresses"] = addresses
        for node in self.nodes:
            self.start(node)
        self.contract()
        self.record("two isolated homeservers ready with validated localhost TLS")

    def bad_certificate(self):
        node = self.nodes[1]
        root = node["root"]
        extension = root / "ca-private/wrong-host.ext"
        extension.write_text("subjectAltName=DNS:wrong.test,DNS:localhost,IP:127.0.0.1\n"
                             "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                             "extendedKeyUsage=serverAuth\n")
        command("openssl", "x509", "-req", "-in", str(root / "ca-private/server.csr"),
                "-CA", str(root / "tls/ca.crt"), "-CAkey", str(root / "ca-private/ca.key"),
                "-set_serial", str(secrets.randbits(128)), "-days", "30", "-sha256",
                "-extfile", str(extension), "-out", str(root / "tls/wrong-host.crt"))
        self.certificate(node, "wrong-host.crt")

    def certificate(self, node, filename):
        config = node["root"] / "config/tuwunel.toml"
        config.write_text(re.sub(r'certs = "/tls/[^"\n]+"', f'certs = "/tls/{filename}"', config.read_text()))
        seal(node["root"], node["meta"])

    def start(self, node):
        dev.compose(node["root"], node["meta"], "up", "-d", "--pull", "never")
        dev.ready(node["root"], node["meta"])
        meta = node["meta"]
        server = dev.container(meta, node["root"])
        relay = dev.container(meta, node["root"], relay=True)
        owned(server, self.state["run_id"])
        owned(relay, self.state["run_id"])
        networks = server["NetworkSettings"]["Networks"]
        dev.require(set(networks) == {dev.identity(meta) + "-net", self.state["networks"][0]},
                    "Unexpected homeserver network attachment")
        for network in networks:
            info = json.loads(command("docker", "network", "inspect", network))[0]
            dev.require(info["Internal"], "Homeserver attached to a non-internal network")
        dev.require(networks[self.state["networks"][0]]["IPAddress"] == node["address"], "Peer IP changed")
        dev.require(not server["HostConfig"]["PortBindings"], "Homeserver published a port directly")
        binding = {"8448/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(meta["port"])}]}
        dev.require(relay["NetworkSettings"]["Ports"] == binding, "Relay did not publish only loopback TLS")
        dev.require("SSL_CERT_FILE=/tls/peers.pem" in server["Config"]["Env"], "Peer CA bundle not configured")

    def stop(self, node):
        dev.compose(node["root"], node["meta"], "stop", "--timeout", "60")
        dev.stopped(dev.container(node["meta"], node["root"]))

    def api(self, node, method, path, body=None, *, user=None, device=None, token=None, expected=(200,)):
        query = {"user_id": user} if user else {}
        if device:
            query["device_id"] = device
        if query:
            path += ("&" if "?" in path else "?") + urlencode(query)
        if user and token is None:
            token = json.loads((node["root"] / "credentials.json").read_text())["as_token"]
        status, data = dev.request(node["root"], node["meta"], method, path, body, token)
        dev.require(status in expected, f"{method} {path.split('?')[0]}: HTTP {status}, code {data.get('errcode', 'none')}")
        return data

    def provision(self, node):
        user = "@terva_probe:" + node["meta"]["server_name"]
        registration = {"type": "m.login.application_service", "username": "terva_probe", "inhibit_login": True}
        result = self.api(node, "POST", PREFIX + "/register", registration, user=user)
        self.record("appservice registration without login token " + node["meta"]["host"],
                    result["user_id"] == user and "access_token" not in result)
        repeated = self.api(node, "POST", PREFIX + "/register", registration, user=user, expected=(400,))
        self.record("registration retry is M_USER_IN_USE", repeated["errcode"] == "M_USER_IN_USE")
        device = "LIFECYCLE"
        for expected in ((201,), (200,)):
            self.api(node, "PUT", PREFIX + "/devices/" + device, {"display_name": "durable probe"}, user=user, expected=expected)
        devices = self.api(node, "GET", PREFIX + "/devices", user=user)["devices"]
        self.record("device PUT retry retains one device", len(devices) == 1 and devices[0]["device_id"] == device)
        node.update(user=user, device=device)
        self.api(node, "PUT", PREFIX + "/profile/" + quote(user, safe="") + "/displayname",
                 {"displayname": "lifecycle peer"}, user=user)
        node["keys"] = self.api(node, "GET", "/_matrix/key/v2/server")["verify_keys"]

    def sync(self, node, since=None):
        query = {"timeout": "0"}
        if since:
            query["since"] = since
        return self.api(node, "GET", PREFIX + "/sync?" + urlencode(query), user=node["user"], device=node["device"])

    def send(self, node, room, text):
        transaction = secrets.token_hex(8)
        path = PREFIX + "/rooms/" + quote(room, safe="") + "/send/m.room.message/" + transaction
        data = self.api(node, "PUT", path, {"msgtype": "m.text", "body": text}, user=node["user"], device=node["device"])
        retry = self.api(node, "PUT", path, {"msgtype": "m.text", "body": text}, user=node["user"], device=node["device"])
        self.record("send retry retains event ID", retry["event_id"] == data["event_id"],
                    sender=node["meta"]["server_name"], event_id=data["event_id"], room_id=room)
        return data["event_id"]

    def delivered(self, node, room, event, since):
        start = time.monotonic()
        def seen():
            nonlocal since
            response = self.sync(node, since)
            since = response["next_batch"]
            timeline = response.get("rooms", {}).get("join", {}).get(room, {}).get("timeline", {})
            dev.require(not timeline.get("limited", False), "Sync timeline was limited; delivery evidence is incomplete")
            return any(item.get("event_id") == event for item in timeline.get("events", []))
        wait_for(seen, self.deadline, "event delivery in recipient sync")
        self.record("recipient sync contains event", elapsed_seconds=round(time.monotonic() - start, 3),
                    recipient=node.get("meta", {}).get("server_name"), event_id=event)

    def contract(self):
        save(self.root / "fixture.json", {"format": 1, "run_id": self.state["run_id"],
            "connector_integration_validated": False, "delivery": "appservice polling", "room_encrypted": False,
            "peers_network": self.state["networks"][0], "nodes": [{
                "directory": str(n["root"]), "endpoint": f"https://localhost:{n['meta']['port']}",
                "container_endpoint": "https://" + n["meta"]["server_name"], "server_name": n["meta"]["server_name"],
                "ca_file": str(n["root"] / "tls/ca.crt"), "credentials_file": str(n["root"] / "credentials.json"),
                "appservice_id": "terva", "sender": "@terva_service:" + n["meta"]["server_name"],
                "namespace": "^@terva_[a-z0-9_]+:" + re.escape(n["meta"]["server_name"]) + "$",
                "user_id": n.get("user"), "device_id": n.get("device"), "room_id": n.get("room"),
                "image": n["meta"]["image"]} for n in self.nodes]})

    def exercise(self):
        a, b = self.nodes
        for node in self.nodes:
            self.provision(node)
        path = PREFIX + "/profile/" + quote(b["user"], safe="")
        self.api(a, "GET", path, expected=(400, 404, 500, 502, 503))
        # Release builds omit the TLS debug log. Establish the certificate defect
        # independently, then change only the leaf certificate and repeat the same API.
        wrong = subprocess.run(["openssl", "verify", "-CAfile", str(b["root"] / "tls/peers.pem"),
            "-verify_hostname", b["meta"]["host"], str(b["root"] / "tls/wrong-host.crt")],
            capture_output=True, text=True, timeout=10)
        self.record("remote profile fails with a trusted but wrong-hostname certificate",
                    wrong.returncode != 0 and "hostname mismatch" in (wrong.stdout + wrong.stderr).lower())
        self.stop(b)
        self.certificate(b, "server.crt")
        self.start(b)
        wait_for(lambda: self.remote_profile(a, path), self.deadline, "valid peer profile after certificate repair")
        self.record("same federation profile request succeeds with valid peer certificate")
        for local, remote in ((a, b), (b, a)):
            for invalid in ("@outside:" + local["meta"]["server_name"], remote["user"]):
                result = self.api(local, "GET", PREFIX + "/account/whoami", user=invalid, expected=(400, 403))
                self.record("namespace rejects " + ("remote identity" if invalid == remote["user"] else "local outsider"),
                            result["errcode"] in ("M_EXCLUSIVE", "M_FORBIDDEN", "M_INVALID_PARAM"))
            other_token = json.loads((remote["root"] / "credentials.json").read_text())["as_token"]
            result = self.api(local, "GET", PREFIX + "/account/whoami", user=local["user"], token=other_token, expected=(401,))
            self.record("other server appservice token rejected", result["errcode"] == "M_UNKNOWN_TOKEN")
        room = self.api(a, "POST", PREFIX + "/createRoom", {"preset": "private_chat"}, user=a["user"], device=a["device"])["room_id"]
        for node in self.nodes:
            node["room"] = room
        room_path = PREFIX + "/rooms/" + quote(room, safe="")
        self.api(a, "GET", room_path + "/state/m.room.encryption", user=a["user"], expected=(404,))
        cursor = self.sync(a)["next_batch"]
        self.delivered(a, room, self.send(a, room, "local message"), cursor)
        self.api(a, "POST", room_path + "/invite", {"user_id": b["user"]}, user=a["user"], device=a["device"])
        self.api(b, "POST", PREFIX + "/join/" + quote(room, safe=""), {}, user=b["user"], device=b["device"])
        for sender, receiver in ((a, b), (b, a)):
            cursor = self.sync(receiver)["next_batch"]
            self.delivered(receiver, room, self.send(sender, room, "federated message"), cursor)
        self.contract()
        cursor = self.sync(b)["next_batch"]
        self.stop(b)
        self.record("peer outage is a clean stop", dev.container(b["meta"], b["root"])["State"]["Status"] == "exited")
        event = self.send(a, room, "queued during peer outage")
        self.record("outage message accepted locally, not yet confirmed delivered", outcome="delayed", event_id=event)
        time.sleep(5)
        self.start(b)
        self.delivered(b, room, event, cursor)
        self.record("outage event delivered without a resend after peer startup", outcome="delivered", event_id=event)
        for node in self.nodes:
            who = self.api(node, "GET", PREFIX + "/account/whoami", user=node["user"], device=node["device"])
            devices = self.api(node, "GET", PREFIX + "/devices", user=node["user"])["devices"]
            members = self.api(node, "GET", room_path + "/joined_members", user=node["user"])["joined"]
            keys = self.api(node, "GET", "/_matrix/key/v2/server")["verify_keys"]
            self.record("persistent identity, device and room membership " + node["meta"]["host"],
                who["user_id"] == node["user"] and who["device_id"] == node["device"] and
                len(devices) == 1 and devices[0]["device_id"] == node["device"] and
                devices[0]["display_name"] == "durable probe" and {a["user"], b["user"]} <= set(members) and keys == node["keys"])
        cursor = self.sync(a)["next_batch"]
        self.delivered(a, room, self.send(b, room, "after peer recovery"), cursor)
        self.record("messaging resumes after peer restart without client resend of outage event")

    def remote_profile(self, node, path):
        status, body = dev.request(node["root"], node["meta"], "GET", path)
        return status == 200 and body.get("displayname") == "lifecycle peer"

    def logs(self):
        for index, node in enumerate(self.nodes):
            try:
                # Logs remain private artifacts, never printed or placed in the report.
                result = subprocess.run(["docker", "logs", "--tail", "300", dev.identity(node["meta"])],
                                        capture_output=True, text=True, timeout=15)
                (self.root / f"server-{index}.log").write_text(result.stdout + result.stderr)
            except (OSError, subprocess.TimeoutExpired):
                pass


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    os.umask(0o077)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    execute = sub.add_parser("run")
    execute.add_argument("directory", help="new directory outside Git; parent must exist")
    execute.add_argument("--image", required=True)
    execute.add_argument("--relay-image", required=True)
    execute.add_argument("--deadline", type=int, default=120, help="delivery/recovery deadline in seconds, 10..300")
    execute.add_argument("--keep", action="store_true", help="retain successful fixture containers for connector-owned tests")
    execute.add_argument("--fail-after-setup", action="store_true", help="exercise assertion failure and cleanup")
    remove = sub.add_parser("cleanup")
    remove.add_argument("directory")
    args = parser.parse_args()
    root = dev.safe_path(args.directory)
    if args.action == "cleanup":
        cleanup(root)
        print("Removed only this run's containers and networks; files retained")
        return 0
    dev.require(10 <= args.deadline <= 300, "Deadline must be 10..300 seconds")
    dev.require(not root.exists(), "Run directory already exists")
    fixture = Fixture(root, args.deadline)
    try:
        fixture.setup(args.image, args.relay_image)
        fixture.record("fault injection disabled", not args.fail_after_setup)
        fixture.exercise()
        fixture.report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        fixture.report["status"] = "failed"
        fixture.report["error"] = str(error) if type(error) is ValueError else type(error).__name__
        print("FAIL " + fixture.report["error"], file=sys.stderr)
    finally:
        if root.exists():
            fixture.logs()
            try:
                if fixture.report["status"] != "passed" or not args.keep:
                    cleanup(root)
                    fixture.report["cleanup"] = "passed"
                else:
                    fixture.report["cleanup"] = "kept by request"
            except Exception as error:
                fixture.report["cleanup"] = "failed: " + (str(error) if type(error) is ValueError else type(error).__name__)
                fixture.report["status"] = "failed"
            fixture.report["finished"] = datetime.now(timezone.utc).isoformat()
            save(root / "results.json", fixture.report)
            print(f"Report: {root / 'results.json'}", flush=True)
    return 0 if fixture.report["status"] == "passed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(str(error) if type(error) is ValueError else type(error).__name__, file=sys.stderr)
        sys.exit(1)
