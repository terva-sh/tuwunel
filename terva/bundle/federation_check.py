#!/usr/bin/env python3
"""Native lifecycle and federation checks: three bundled servers on loopback,
each under its own test CA, run as plain processes.

  federation_check.py --binary PATH --work DIR [--deadline SECONDS]

Servers a and b trust each other's root through `trusted_roots` and must
provision, reject foreign identities, federate both ways, survive a wrong
peer certificate, deliver a message queued during a peer outage, keep their
identity across the restart, and reload a reissued certificate in place.
Server c is trusted by nobody and must be refused.

DIR must be new and outside Git. Everything generated is test-only and stays
there. Exit status is nonzero on any failed assertion. Tickets TKT-01M2ZV04F4
(trusted_roots and reload) and TKT-01M2ZMEZEQ (native lifecycle checks).
"""

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import bundle

PREFIX = bundle.PREFIX


def presented_serial(port, cafile, host):
    context = ssl.create_default_context(cafile=cafile)
    with socket.create_connection(("127.0.0.1", port), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.getpeercert()["serialNumber"].upper().lstrip("0")


def until(predicate, seconds, step=1.0):
    deadline = time.monotonic() + seconds
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(step)


class Node:
    def __init__(self, checker, label, record):
        self.checker, self.label, self.record = checker, label, record
        self.root = checker.root
        self.name = checker.meta["server_name"]
        self.port = checker.meta["port"]
        self.user = self.device = None
        self.keys = None

    @property
    def pid(self):
        return self.checker.process.pid

    def api(self, *args, **kwargs):
        return self.checker.api(*args, **kwargs)

    def provision(self, localpart, device):
        self.user, self.device = f"@{localpart}:{self.name}", device
        body = {"type": "m.login.application_service", "username": localpart, "inhibit_login": True}
        status, data = self.api("POST", f"{PREFIX}/register", body)
        self.record(f"{self.label}: registration without a login token",
                    status == 200 and data.get("user_id") == self.user and "access_token" not in data, status=status)
        status, data = self.api("POST", f"{PREFIX}/register", body)
        self.record(f"{self.label}: registration retry is M_USER_IN_USE",
                    status == 400 and data.get("errcode") == "M_USER_IN_USE", status=status)
        first, _ = self.api("PUT", f"{PREFIX}/devices/{quote(device)}", {"display_name": "durable probe"}, user=self.user)
        second, _ = self.api("PUT", f"{PREFIX}/devices/{quote(device)}", {"display_name": "durable probe"}, user=self.user)
        status, data = self.api("GET", f"{PREFIX}/devices", user=self.user)
        devices = data.get("devices", [])
        self.record(f"{self.label}: device PUT retry retains one device",
                    (first, second) == (201, 200) and len(devices) == 1 and devices[0].get("device_id") == device,
                    codes=[first, second], devices=len(devices))
        self.api("PUT", f"{PREFIX}/profile/{quote(self.user, safe='')}/displayname", {"displayname": "lifecycle peer"},
                 user=self.user)
        status, data = self.api("GET", "/_matrix/key/v2/server", auth=False)
        self.keys = data.get("verify_keys")
        self.record(f"{self.label}: publishes signing keys", status == 200 and bool(self.keys))

    def sync(self, since=None):
        path = f"{PREFIX}/sync?timeout=0&set_presence=offline" + (f"&since={quote(since, safe='')}" if since else "")
        status, data = self.api("GET", path, user=self.user, device=self.device)
        bundle.require(status == 200, f"{self.label}: sync failed ({status})")
        return data

    def cursor(self):
        return self.sync()["next_batch"]

    def send(self, room, text):
        txn = secrets.token_hex(8)
        path = f"{PREFIX}/rooms/{quote(room, safe='')}/send/m.room.message/{txn}"
        body = {"msgtype": "m.text", "body": text}
        status, data = self.api("PUT", path, body, user=self.user, device=self.device)
        bundle.require(status == 200 and data.get("event_id", "").startswith("$"), f"{self.label}: send failed ({status})")
        _, retry = self.api("PUT", path, body, user=self.user, device=self.device)
        self.record(f"{self.label}: send retry retains the event ID", retry.get("event_id") == data["event_id"])
        return data["event_id"]

    def delivered(self, room, event, since, deadline):
        """Advances a sync cursor only after each successful response and never
        calls an endpoint that could fetch the event on demand."""
        began = time.monotonic()

        def seen():
            nonlocal since
            response = self.sync(since)
            since = response["next_batch"]
            timeline = response.get("rooms", {}).get("join", {}).get(room, {}).get("timeline", {})
            bundle.require(not timeline.get("limited", False), "sync timeline was limited; delivery evidence incomplete")
            return any(e.get("event_id") == event for e in timeline.get("events", []))

        return until(seen, deadline), round(time.monotonic() - began, 1)

    def remote_profile_ok(self, user):
        status, _ = self.api("GET", f"{PREFIX}/profile/{quote(user, safe='')}", user=self.user)
        return status == 200


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--base-port", type=int, default=18448)
    parser.add_argument("--deadline", type=int, default=120, help="seconds allowed for federated delivery")
    args = parser.parse_args(argv)
    bundle.require(10 <= args.deadline <= 300, "--deadline must be between 10 and 300")

    work = bundle.safe_root(args.work)
    bundle.require(not work.exists(), "--work must be a new directory")
    work.mkdir(mode=0o700)
    ports = {label: args.base_port + i for i, label in enumerate(("a", "b", "c"))}
    cas = {label: bundle.ca_init(work / f"ca-{label}", f"Federation check CA {label.upper()}") for label in ports}

    # a and b trust each other. c trusts a but nobody trusts c: the negative case.
    trust = {"a": ["b"], "b": ["a"], "c": ["a"]}
    for label, port in ports.items():
        init = ["init", str(work / label), "--binary", args.binary, "--server-name", f"localhost:{port}",
                "--mode", "federated", "--address", "127.0.0.1", "--port", str(port), "--ca", str(cas[label]),
                "--fixture"]
        for peer in trust[label]:
            init += ["--trust", str(cas[peer] / "ca.crt")]
        bundle.require(bundle.main(init) == 0, f"init {label} failed")

    results = []

    def record(name, condition, **details):
        results.append({"name": name, "ok": bool(condition), **details})
        print(("ok   " if condition else "FAIL ") + name + (f"  {details}" if details else ""))
        return condition

    nodes = {}
    for label in ports:
        root, meta, binary = bundle.load(work / label)
        nodes[label] = Node(bundle.Check(root, meta, binary), label, record)
    a, b, c = nodes["a"], nodes["b"], nodes["c"]

    try:
        for node in nodes.values():
            bundle.require(node.checker.start(), f"{node.label} did not start")

        # Provisioning and its retries.
        a.provision("terva_alpha", "ALPHADEV1")
        b.provision("terva_beta", "BETADEV1")
        c.provision("terva_gamma", "GAMMADEV1")

        # Namespace isolation: a token never reaches outside its own namespace,
        # its own server, or its own registration.
        for local, remote in ((a, b), (b, a)):
            for kind, ident in (("local outsider", f"@outside:{local.name}"), ("remote identity", remote.user)):
                status, data = local.api("GET", f"{PREFIX}/account/whoami", user=ident)
                record(f"{local.label}: namespace rejects {kind}",
                       status in (400, 403) and data.get("errcode") in ("M_EXCLUSIVE", "M_FORBIDDEN", "M_INVALID_PARAM"),
                       status=status, error=data.get("errcode"))
            status, data = local.api("GET", f"{PREFIX}/account/whoami", user=local.user, token=remote.checker.token)
            record(f"{local.label}: other server's appservice token rejected",
                   status == 401 and data.get("errcode") == "M_UNKNOWN_TOKEN", status=status)

        # A plaintext room, a local message, then federation in both directions.
        status, data = a.api("POST", f"{PREFIX}/createRoom", {"preset": "private_chat", "name": "lifecycle check",
                                                               "initial_state": []}, user=a.user, device=a.device)
        room = data.get("room_id", "")
        record("a: creates a room", status == 200 and room.startswith("!"), status=status)
        encoded = quote(room, safe="")
        status, data = a.api("GET", f"{PREFIX}/rooms/{encoded}/state/m.room.encryption/", user=a.user, device=a.device)
        record("room is plaintext", status == 404 and data.get("errcode") == "M_NOT_FOUND")
        since = a.cursor()
        ok, seconds = a.delivered(room, a.send(room, "local message"), since, args.deadline)
        record("a: local message in own sync", ok, seconds=seconds)

        # Negative control on a's outbound verification, before b shares a room
        # with a (after that, a answers profile requests from room state and
        # never federates). b presents a certificate from its trusted authority
        # but for the wrong host, swapped in and back out through the reload
        # signal, so b's process never restarts.
        with tempfile.TemporaryDirectory() as temp:
            bundle.ca_issue(cas["b"], "wrong.test", Path(temp))
            verify = subprocess.run(["openssl", "verify", "-CAfile", str(cas["b"] / "ca.crt"), "-verify_hostname",
                                     "localhost", f"{temp}/server.crt"], capture_output=True, text=True, timeout=10)
            record("wrong-host certificate fails hostname verification independently",
                   verify.returncode != 0 and "hostname mismatch" in (verify.stdout + verify.stderr).lower())
            good = {name: (b.root / "tls" / name).read_bytes() for name in ("server.crt", "server.key")}
            for name in good:
                shutil.copyfile(f"{temp}/{name}", b.root / "tls" / name)
            os.kill(b.pid, signal.SIGUSR1)
            time.sleep(3)  # past request_idle_timeout, so a cannot reuse a connection that saw the old certificate
            swapped = until(lambda: presented_serial(b.port, str(cas["b"] / "ca.crt"), "127.0.0.1")
                            == bundle.cert_serial(Path(temp) / "server.crt").upper().lstrip("0"), 15)
            record("b: wrong-host certificate reloaded in place", bool(swapped))
            # Three probes over several seconds, all failing, rather than one that
            # could fail for an unrelated transient reason.
            probes = [a.remote_profile_ok(b.user) for _ in range(3) if not time.sleep(2)]
            record("a: remote profile fails against the wrong-host certificate", not any(probes), probes=probes)
            for name, data in good.items():
                (b.root / "tls" / name).write_bytes(data)
            os.kill(b.pid, signal.SIGUSR1)
            record("a: same remote profile request succeeds after the certificate is repaired",
                   until(lambda: a.remote_profile_ok(b.user), args.deadline))


        status, data = a.api("POST", f"{PREFIX}/rooms/{encoded}/invite", {"user_id": b.user}, user=a.user, device=a.device)
        record("a invites b over federation", status == 200, status=status, error=data.get("errcode"),
               detail=str(data.get("error", ""))[:160])
        status, data = b.api("POST", f"{PREFIX}/join/{encoded}?server_name={quote(a.name, safe='')}", {},
                             user=b.user, device=b.device)
        record("b joins over federation", status == 200 and data.get("room_id") == room, status=status,
               error=data.get("errcode"), detail=str(data.get("error", ""))[:160])
        for sender, receiver in ((a, b), (b, a)):
            since = receiver.cursor()
            ok, seconds = receiver.delivered(room, sender.send(room, "federated message"), since, args.deadline)
            record(f"{sender.label} to {receiver.label}: federated message delivered", ok, seconds=seconds)

        # Trust is the only thing separating b from c.
        status, data = a.api("POST", f"{PREFIX}/rooms/{encoded}/invite", {"user_id": c.user}, user=a.user, device=a.device)
        detail = str(data.get("error", ""))
        record("a: invite to a peer under an untrusted authority is refused",
               status != 200 and "request" in detail.lower() and data.get("errcode") != "M_NOT_FOUND",
               status=status, error=data.get("errcode"), detail=detail[:160])

        # Peer outage: a message accepted while b is down must arrive from a's
        # queued sender after b restarts on its original data, with no resend.
        since = b.cursor()
        b.checker.stop()
        record("b: peer outage is a clean stop", b.checker.results[-1]["ok"] and b.checker.results[-1]["name"] == "stop")
        queued = a.send(room, "queued during peer outage")
        record("a: outage message accepted locally while b is down",
               b.checker.process.poll() is not None and queued.startswith("$"), outcome="delayed")
        time.sleep(5)
        bundle.require(b.checker.start(), "b did not restart")
        ok, seconds = b.delivered(room, queued, since, args.deadline)
        record("b: outage message delivered after restart without a resend", ok, seconds=seconds)

        for node in (a, b):
            _, who = node.api("GET", f"{PREFIX}/account/whoami", user=node.user, device=node.device)
            _, devs = node.api("GET", f"{PREFIX}/devices", user=node.user)
            _, members = node.api("GET", f"{PREFIX}/rooms/{encoded}/joined_members", user=node.user)
            _, keys = node.api("GET", "/_matrix/key/v2/server", auth=False)
            devices = devs.get("devices", [])
            record(f"{node.label}: identity, device, membership and signing keys persist",
                   who.get("user_id") == node.user and who.get("device_id") == node.device
                   and len(devices) == 1 and devices[0].get("device_id") == node.device
                   and devices[0].get("display_name") == "durable probe"
                   and {a.user, b.user} <= set(members.get("joined", {}))
                   and keys.get("verify_keys") == node.keys)
        since = a.cursor()
        ok, seconds = a.delivered(room, b.send(room, "after peer recovery"), since, args.deadline)
        record("b to a: messaging resumes after the peer restart", ok, seconds=seconds)

        # Renewal: reissue a's certificate and reload it under the same PID.
        pid = a.pid
        before = presented_serial(a.port, str(cas["a"] / "ca.crt"), "localhost")
        bundle.main(["renew", str(work / "a"), "--pid", str(pid)])
        expected = bundle.cert_serial(work / "a" / "tls" / "server.crt").upper().lstrip("0")
        after = until(lambda: (lambda s: s if s != before else None)(
            presented_serial(a.port, str(cas["a"] / "ca.crt"), "localhost")), 15)
        record("a: reissued certificate presented by the same process", after == expected and a.pid == pid,
               before=before[:8], after=(after or "")[:8], pid=pid)
        since = a.cursor()
        ok, seconds = a.delivered(room, b.send(room, "after certificate reload"), since, args.deadline)
        record("b to a: federation continues after the reload", ok, seconds=seconds)
    finally:
        for node in nodes.values():
            node.checker.stop()
            if node.checker.process and node.checker.process.poll() is None:
                node.checker.process.kill()
                node.checker.process.wait()
        for node in nodes.values():
            node.checker.report()

    ok = all(r["ok"] for r in results)
    (work / "report.json").write_text(json.dumps({"ok": ok, "results": results}, indent=2) + "\n")
    print(("PASS" if ok else "FAIL") + f": {sum(r['ok'] for r in results)}/{len(results)}, report in {work / 'report.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(2)
