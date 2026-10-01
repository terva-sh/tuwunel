#!/usr/bin/env python3
"""Seed or read back a disposable deployment fixture. Not a connector or crypto test."""

import argparse
import json
import os
import secrets
import ssl
import sys
from urllib.parse import quote, urlencode

import dev

PREFIX = "/_matrix/client/v3"
MEDIA = b"Terva development snapshot media\x00\xff\n"


def isolation(root, meta):
    item = dev.container(meta, root)
    dev.require(item is not None and item["State"]["Running"], "Homeserver is not running")
    config = item["HostConfig"]
    dev.require(not config["PortBindings"], "Homeserver must not publish ports directly")
    dev.require(config["ReadonlyRootfs"] and config["CapDrop"] == ["ALL"] and not config["Privileged"],
                "Runtime privileges differ from the recipe")
    dev.require(config["Memory"] == 2 * 1024**3 and config["PidsLimit"] == 512 and config["NanoCpus"] == 2 * 10**9,
                "Runtime resource limits differ from the recipe")
    dev.require(item["Config"]["User"] == f"{meta['uid']}:{meta['gid']}", "Unexpected runtime user")
    network_name = dev.identity(meta) + "-net"
    dev.require(list(item["NetworkSettings"]["Networks"]) == [network_name], "Unexpected network attachment")
    network = json.loads(dev.run("docker", "network", "inspect", network_name))[0]
    dev.require(network["Internal"], "Test network is not internal")
    relay = dev.container(meta, root, relay=True)
    dev.require(relay is not None and relay["State"]["Running"], "TLS relay is not running")
    binding = {"8448/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(meta["port"])}]}
    dev.require(relay["HostConfig"]["PortBindings"] == binding and relay["NetworkSettings"]["Ports"] == binding,
                "Relay did not publish only the loopback TLS port")
    dev.require(relay["HostConfig"]["Sysctls"]["net.ipv4.ip_forward"] == "0", "Relay IP forwarding is enabled")
    dev.require(len(relay["Mounts"]) == 1 and relay["Mounts"][0]["Source"] == str(root / "config/haproxy.cfg"),
                "Relay mounted more than its non-secret configuration")


def probe_container(root, meta, image):
    image_id = dev.run("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()
    code = '''import http.client, socket, ssl, sys
host = sys.argv[1]
context = ssl.create_default_context(cafile="/ca.crt")
connection = http.client.HTTPSConnection(host, 8448, context=context, timeout=5)
connection.request("GET", "/.well-known/matrix/server")
response = connection.getresponse()
assert response.status == 200
response.read()
connection.close()
for ctx, name in [(ssl.create_default_context(), host), (context, "wrong.test")]:
    try:
        with socket.create_connection((host, 8448), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname=name):
                raise AssertionError("Invalid trust or hostname accepted")
    except ssl.SSLCertVerificationError:
        pass
print("PASS: container DNS, private CA trust and hostname rejection")
'''
    result = dev.run("docker", "run", "--rm", "--pull", "never", "--read-only", "--cap-drop", "ALL",
                     "--security-opt", "no-new-privileges", "--user", f"{meta['uid']}:{meta['gid']}",
                     "--memory", "128m", "--cpus", "0.5", "--pids-limit", "64",
                     "--network", dev.identity(meta) + "-net", "--mount",
                     f'type=bind,source={root / "tls/ca.crt"},target=/ca.crt,readonly',
                     "--entrypoint", "python", image_id, "-B", "-c", code, meta["host"])
    print(result.strip())


def verify(root, seed):
    meta = dev.load(root)
    dev.ready(root, meta)
    token = json.loads((root / "credentials.json").read_text())["as_token"]

    def api(method, path, body=None, user=None, device=None, expected=(200,), raw=False):
        query = {"user_id": user} if user else {}
        if device:
            query["device_id"] = device
        if query:
            path += "?" + urlencode(query)
        status, result = dev.request(root, meta, method, path, body, token if user else None, raw=raw)
        dev.require(status in expected, f"{method} {path.split('?')[0]} returned HTTP {status}")
        return result

    try:
        dev.request(root, meta, "GET", "/_matrix/client/versions", trust=False)
    except ssl.SSLCertVerificationError:
        pass
    else:
        raise ValueError("System trust accepted the private CA; use an isolated test host without that CA installed")
    client = api("GET", "/.well-known/matrix/client")
    server = api("GET", "/.well-known/matrix/server")
    dev.require(client["m.homeserver"]["base_url"].rstrip("/") == "https://" + meta["server_name"], "Client discovery mismatch")
    dev.require(server["m.server"] == meta["server_name"], "Federation discovery mismatch")
    keys = api("GET", "/_matrix/key/v2/server")
    dev.require(keys["server_name"] == meta["server_name"], "Signing-key identity mismatch")
    api("GET", "/_matrix/federation/v1/version")
    fixture_path = root / "fixture.json"
    if seed:
        dev.require(not fixture_path.exists(), "Fixture already exists; use check")
        suffix = secrets.token_hex(6)
        localpart = "terva_probe_" + suffix
        user = "@" + localpart + ":" + meta["server_name"]
        device = "PROBE_" + suffix
        denied = api("POST", PREFIX + "/register", {"username": "public_" + suffix, "inhibit_login": True,
                                                     "auth": {"type": "m.login.dummy"}}, expected=(403,))
        dev.require(denied["errcode"] == "M_FORBIDDEN", "Public registration was not disabled")
        registration = api("POST", PREFIX + "/register", {"type": "m.login.application_service",
                           "username": localpart, "inhibit_login": True}, user=user)
        dev.require(registration["user_id"] == user and "access_token" not in registration, "Unexpected appservice registration response")
        joined = api("GET", PREFIX + "/joined_rooms", user=user)
        dev.require(joined["joined_rooms"] == [], "Fresh virtual user unexpectedly joined a room")
        api("PUT", PREFIX + "/devices/" + device, {"display_name": "snapshot probe"}, user=user, expected=(201,))
        room = api("POST", PREFIX + "/createRoom", {"preset": "private_chat", "name": "Snapshot probe"},
                   user=user, device=device)["room_id"]
        room_path = PREFIX + "/rooms/" + quote(room, safe="")
        api("GET", room_path + "/state/m.room.encryption", user=user, device=device, expected=(404,))
        marker = "snapshot-message-" + suffix
        event = api("PUT", room_path + "/send/m.room.message/" + suffix,
                    {"msgtype": "m.text", "body": marker}, user=user, device=device)["event_id"]
        encrypted = api("POST", PREFIX + "/createRoom", {"preset": "private_chat", "initial_state": [
            {"type": "m.room.encryption", "state_key": "", "content": {"algorithm": "m.megolm.v1.aes-sha2"}}]},
            user=user, device=device)["room_id"]
        mxc = api("POST", "/_matrix/media/v3/upload", MEDIA, user=user, device=device)["content_uri"]
        dev.put_json(fixture_path, {"user": user, "device": device, "room": room, "event": event,
                                   "marker": marker, "encrypted_room": encrypted, "media": mxc,
                                   "verify_keys": keys["verify_keys"]})
    fixture = json.loads(fixture_path.read_text())
    user, device = fixture["user"], fixture["device"]
    who = api("GET", PREFIX + "/account/whoami", user=user, device=device)
    dev.require(who["user_id"] == user and who["device_id"] == device, "User/device assertion mismatch")
    details = api("GET", PREFIX + "/devices/" + device, user=user, device=device)
    dev.require(details["display_name"] == "snapshot probe", "Device metadata changed")
    room_path = PREFIX + "/rooms/" + quote(fixture["room"], safe="")
    event = api("GET", room_path + "/event/" + quote(fixture["event"], safe=""), user=user, device=device)
    dev.require(event["content"]["body"] == fixture["marker"], "Room event changed")
    encrypted_path = PREFIX + "/rooms/" + quote(fixture["encrypted_room"], safe="")
    encryption = api("GET", encrypted_path + "/state/m.room.encryption", user=user, device=device)
    dev.require(encryption["algorithm"] == "m.megolm.v1.aes-sha2", "Encryption state changed")
    media_path = "/_matrix/client/v1/media/download/" + fixture["media"].removeprefix("mxc://")
    content = api("GET", media_path, user=user, device=device, raw=True)
    dev.require(content == MEDIA, "Media contents changed")
    dev.require(keys["verify_keys"] == fixture["verify_keys"], "Server signing identity changed")
    print("PASS: explicit TLS trust, discovery, signing identity, appservice user/device, room event, encryption state and media")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "check"))
    parser.add_argument("directory")
    parser.add_argument("--allow-mutation", action="store_true")
    parser.add_argument("--probe-image", help="optional local Python 3 image for an isolated container TLS probe")
    args = parser.parse_args()
    dev.require(args.mode != "seed" or args.allow_mutation, "Seed requires --allow-mutation on a disposable instance")
    root = dev.safe_path(args.directory)
    dev.require(not (root / "snapshot.json").exists(), "Restore the snapshot before testing it")
    meta = dev.load(root)
    with dev.locked(meta):
        isolation(root, meta)
        verify(root, args.mode == "seed")
        if args.probe_image:
            probe_container(root, meta, args.probe_image)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print(str(error) if type(error) is ValueError else type(error).__name__, file=sys.stderr)
        sys.exit(1)
