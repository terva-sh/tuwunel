"""Test curl request construction against a loopback recorder, not a homeserver.

Run with: uv run --no-project python -m unittest discover -s terva/contract -v
No tuwunel, connector, credentials, or external network is used.
"""

import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "terva/contract/requests.sh"
DOCUMENT = ROOT / "terva/docs/homeserver-contract.md"
TOKEN = "synthetic-test-token-not-a-credential"


class Recorder(BaseHTTPRequestHandler):
    def record(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.put({
            "method": self.command,
            "target": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "body": json.loads(body) if body else None,
        })
        payload = json.dumps({"errcode": "M_NOT_FOUND"} if self.server.status == 404 else {}).encode()
        self.send_response(self.server.status)
        if self.server.status == 302:
            self.send_header("Location", "/must-not-follow")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = record

    def log_message(self, *_args):
        pass


class RequestsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
        cls.server.requests = queue.Queue()
        cls.server.status = 200
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.status = 200
        while not self.server.requests.empty():
            self.server.requests.get_nowait()
        self.inputs = {
            "HS": f"http://127.0.0.1:{self.server.server_port}",
            "ALLOW_LOOPBACK_HTTP": "1",
            "ALLOW_MUTATION": "1",
            "AS_TOKEN": TOKEN,
            "USER_ID": "@terva_a:example.test",
            "DEVICE_ID": "device /?&=+",
            "LOCALPART": "terva_a",
            "ROOM_ID": "!room/with+symbols:example.test",
            "INVITEE": '@terva_b:example.test',
            "ENCRYPTED": "false",
            "TXN_ID": "txn/one?&=+",
            "PLAINTEXT_ROOM": "!room/with+symbols:example.test",
            "CONFIRM_DELETE_DEVICE": "@terva_a:example.test/device /?&=+",
            "SINCE": "",
        }

    def invoke(self, operation, **overrides):
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
        env.update(self.inputs)
        env.update(overrides)
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        return subprocess.run(["bash", str(SCRIPT), operation], env=env,
                              capture_output=True, text=True, timeout=10)

    def request(self, operation, **overrides):
        result = self.invoke(operation, **overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.server.requests.get(timeout=2)
        self.assertTrue(self.server.requests.empty(), "Expected one request only")
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        self.assertNotIn(TOKEN, request["target"])
        parsed = urlsplit(request["target"])
        request["path"] = parsed.path
        request["query"] = parse_qs(parsed.query)
        if operation == "versions":
            self.assertIsNone(request["authorization"])
        else:
            self.assertEqual(request["authorization"], f"Bearer {TOKEN}")
        if request["body"] is not None:
            self.assertEqual(request["content_type"], "application/json")
        return request

    def test_versions_is_unauthenticated(self):
        request = self.request("versions", AS_TOKEN="", ALLOW_MUTATION="")
        self.assertEqual(request["path"], "/_matrix/client/versions")
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["query"], {})

    def test_register_does_not_assert_an_uncreated_user_or_device(self):
        request = self.request("register", USER_ID="", DEVICE_ID="")
        self.assertEqual(request["path"], "/_matrix/client/v3/register")
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["query"], {})
        self.assertEqual(request["body"], {
            "type": "m.login.application_service", "username": "terva_a", "inhibit_login": True,
        })

    def test_device_management_omits_device_assertion_and_encodes_path(self):
        for operation, method in [("put-device", "PUT"), ("get-device", "GET"),
                                  ("delete-device", "DELETE")]:
            with self.subTest(operation=operation):
                request = self.request(operation)
                self.assertEqual(request["method"], method)
                self.assertEqual(request["path"], "/_matrix/client/v3/devices/" +
                                 quote(self.inputs["DEVICE_ID"], safe=""))
                self.assertEqual(request["query"], {"user_id": [self.inputs["USER_ID"]]})

    def test_create_room_requires_explicit_encryption_and_builds_both_payloads(self):
        for encrypted in ("true", "false"):
            with self.subTest(encrypted=encrypted):
                request = self.request("create-room", ENCRYPTED=encrypted)
                self.assertEqual(request["path"], "/_matrix/client/v3/createRoom")
                self.assertEqual(request["method"], "POST")
                self.assertEqual(request["body"]["preset"], "private_chat")
                state = request["body"]["initial_state"]
                self.assertEqual(state, [{"type": "m.room.encryption", "state_key": "",
                                         "content": {"algorithm": "m.megolm.v1.aes-sha2"}}]
                                 if encrypted == "true" else [])
        for encrypted in ("", "null", "yes", "{}"):
            with self.subTest(invalid=encrypted):
                self.assertRefused("create-room", ENCRYPTED=encrypted)

    def test_member_requests_assert_the_device(self):
        room = quote(self.inputs["ROOM_ID"], safe="")
        cases = {
            "whoami": ("GET", "/account/whoami"),
            "devices": ("GET", "/devices"),
            "joined-rooms": ("GET", "/joined_rooms"),
            "invite": ("POST", f"/rooms/{room}/invite"),
            "join": ("POST", f"/join/{room}"),
            "leave": ("POST", f"/rooms/{room}/leave"),
            "members": ("GET", f"/rooms/{room}/joined_members"),
            "encryption": ("GET", f"/rooms/{room}/state/m.room.encryption/"),
        }
        for operation, (method, path) in cases.items():
            with self.subTest(operation=operation):
                request = self.request(operation)
                self.assertEqual(request["method"], method)
                self.assertEqual(request["path"], "/_matrix/client/v3" + path)
                self.assertEqual(request["query"], {
                    "user_id": [self.inputs["USER_ID"]],
                    "device_id": [self.inputs["DEVICE_ID"]],
                })
                if operation == "invite":
                    self.assertEqual(request["body"], {"user_id": self.inputs["INVITEE"]})

    def test_sync_keeps_opaque_since_and_query_parameters_separate(self):
        since = "opaque +&?=/token"
        request = self.request("sync", SINCE=since)
        self.assertEqual(request["path"], "/_matrix/client/v3/sync")
        self.assertEqual(request["query"]["since"], [since])
        self.assertEqual(request["query"]["timeout"], ["0"])
        self.assertEqual(request["query"]["set_presence"], ["offline"])
        self.assertNotIn("since", self.request("sync")["query"])

    def test_send_keeps_transaction_id_and_payload_on_repeat(self):
        first = self.request("send")
        second = self.request("send")
        self.assertEqual(first, second)
        self.assertEqual(first["method"], "PUT")
        self.assertTrue(first["path"].endswith("/send/m.room.message/" +
                                             quote(self.inputs["TXN_ID"], safe="")))
        self.assertEqual(first["body"], {
            "msgtype": "m.text", "body": "terva contract probe, synthetic content only",
        })

    def assertRefused(self, operation, **overrides):
        result = self.invoke(operation, **overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.server.requests.empty(), "Refused operation made a request")
        self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_mutations_require_opt_in(self):
        for operation in ("register", "put-device", "delete-device", "create-room",
                          "invite", "join", "leave", "send"):
            with self.subTest(operation=operation):
                self.assertRefused(operation, ALLOW_MUTATION="")

    def test_delete_and_plaintext_send_need_exact_confirmation(self):
        self.assertRefused("delete-device", CONFIRM_DELETE_DEVICE="wrong-user/device")
        self.assertRefused("send", PLAINTEXT_ROOM="!other:example.test")
        self.assertRefused("send", TXN_ID="")

    def test_transport_and_header_guards(self):
        self.assertRefused("versions", ALLOW_LOOPBACK_HTTP="")
        for origin in ("http://remote.example.test", "https://user:secret@example.test",
                       "https://example.test?token=x", "https://example.test/path",
                       "https://example.test/#fragment", "file:///tmp/nope"):
            with self.subTest(origin=origin):
                self.assertRefused("whoami", HS=origin)
        self.assertRefused("whoami", AS_TOKEN="fake\r\nInjected: header")
        self.assertRefused("whoami", DEVICE_ID="")
        self.assertRefused("register", LOCALPART="")
        self.assertRefused("unknown")

    def test_http_errors_stay_errors_with_matrix_body(self):
        self.server.status = 404
        result = self.invoke("encryption")
        self.assertEqual(result.returncode, 22, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"errcode": "M_NOT_FOUND"})
        self.assertIn("HTTP 404", result.stderr)
        self.server.requests.get(timeout=2)
        self.assertTrue(self.server.requests.empty())

    def test_redirect_is_not_followed(self):
        self.server.status = 302
        self.request("whoami")


class DocumentTest(unittest.TestCase):
    def test_registration_fragment_and_encryption_event(self):
        text = DOCUMENT.read_text()
        fragment = re.search(r"```toml\n(.*?)```", text, re.S).group(1)
        registration = tomllib.loads(fragment)["global"]["appservice"]["terva"]
        self.assertTrue(registration["device_management"])
        self.assertNotIn("url", registration)
        namespace = registration["users"][0]
        self.assertTrue(namespace["exclusive"])
        self.assertIsNotNone(re.fullmatch(namespace["regex"], "@terva_a:example.test"))
        self.assertIsNone(re.fullmatch(namespace["regex"], "@terva_a:other.test"))
        event = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
        self.assertEqual(event["content"]["algorithm"], "m.megolm.v1.aes-sha2")

    def test_source_paths_and_document_links_exist(self):
        text = DOCUMENT.read_text()
        for path in re.findall(r"`(src/[^`]+\.rs)`", text):
            with self.subTest(source=path):
                self.assertTrue((ROOT / path).is_file())
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith("https://"):
                continue
            with self.subTest(link=target):
                self.assertTrue((DOCUMENT.parent / target).resolve().is_file())


if __name__ == "__main__":
    unittest.main()
