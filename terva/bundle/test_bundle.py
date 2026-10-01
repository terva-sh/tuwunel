"""Offline tests for the bundled profile tooling. No binary and no network."""

import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import tomllib
import unittest
from unittest import mock
import contextlib
import io
import types

import bundle

HERE = Path(__file__).resolve().parent


def meta(**overrides):
    base = {"format": 1, "mode": "local", "server_name": "agents.example", "address": "127.0.0.1", "port": 8008,
            "appservice_id": "terva", "sender_localpart": "terva_service", "namespace_prefix": "terva_"}
    base.update(overrides)
    return base


def proxied(**overrides):
    return meta(**{"mode": "proxied", "public_host": "matrix.agents.example", "peer_networks": [], **overrides})


class ProfileTests(unittest.TestCase):
    def load(self, name):
        with (HERE / "profile" / f"{name}.toml").open("rb") as stream:
            return tomllib.load(stream)["global"]

    def test_profiles_parse_and_share_the_fixed_policy(self):
        for name in bundle.MODES:
            with self.subTest(mode=name):
                profile = self.load(name)
                self.assertFalse(profile["allow_registration"])
                self.assertFalse(profile["allow_guest_registration"])
                self.assertFalse(profile["grant_admin_to_first_user"])
                self.assertTrue(profile["allow_encryption"])
                self.assertNotIn("encryption_enabled_by_default_for_room_type", profile)
                self.assertFalse(profile["log_colors"])
                for key in ("server_name", "database_path", "port", "appservice"):
                    self.assertNotIn(key, profile, f"{key} belongs to the per-deployment file")

    def test_local_profile_is_loopback_and_isolated(self):
        profile = self.load("local")
        self.assertEqual(profile["address"], "127.0.0.1")
        self.assertFalse(profile["allow_federation"])
        self.assertEqual(profile["trusted_servers"], [])
        self.assertNotIn("tls", profile)

    def test_federated_profile_verifies_peers_and_is_https_only(self):
        profile = self.load("federated")
        self.assertTrue(profile["allow_federation"])
        self.assertFalse(profile["allow_invalid_tls_certificates"])
        self.assertFalse(profile["tls"]["dual_protocol"])
        self.assertNotIn("address", profile, "the deployment file chooses the listener")

    def test_proxied_profile_is_loopback_and_federates_without_a_notary(self):
        profile = self.load("proxied")
        self.assertEqual(profile["address"], "127.0.0.1")
        self.assertTrue(profile["allow_federation"])
        self.assertEqual(profile["trusted_servers"], [])
        self.assertFalse(profile["allow_invalid_tls_certificates"])
        self.assertNotIn("tls", profile, "the proxy terminates TLS")
        self.assertNotIn("ip_source", profile, "loopback peers bypass it, so it would mislead")

    def test_default_denylist_matches_the_server_source(self):
        schema = (HERE.parents[1] / "src" / "core" / "config" / "mod.rs").read_text()
        body = schema.split("fn default_ip_range_denylist()", 1)[1].split("}", 1)[0]
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', body)), bundle.DEFAULT_DENYLIST)

    def test_profile_keys_exist_in_the_example_config(self):
        # The generated example hides a few options; the schema source has them all.
        example = (HERE.parents[1] / "tuwunel-example.toml").read_text()
        schema = (HERE.parents[1] / "src" / "core" / "config" / "mod.rs").read_text()
        known = set(re.findall(r"^#?([a-z_]+) *=", example, re.MULTILINE))
        known |= set(re.findall(r"^\s*pub ([a-z_]+):", schema, re.MULTILINE))
        for name in bundle.MODES:
            with self.subTest(mode=name):
                profile = self.load(name)
                keys = {k for k, v in profile.items() if not isinstance(v, dict)} | set(profile.get("tls", {}))
                self.assertLessEqual(keys, known, "a profile names a setting the example config does not")


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"

    def test_deployment_file_local(self):
        text = bundle.deployment_text(meta(), self.root)
        parsed = tomllib.loads(text)["global"]
        self.assertEqual(parsed["server_name"], "agents.example")
        self.assertEqual(parsed["database_path"], str(self.root / "data"))
        self.assertEqual(parsed["port"], 8008)
        self.assertNotIn("tls", parsed)
        self.assertNotIn("well_known", parsed)

    def test_deployment_file_federated_publishes_the_listener_port(self):
        text = bundle.deployment_text(meta(mode="federated", address="0.0.0.0", port=8448), self.root)
        parsed = tomllib.loads(text)["global"]
        self.assertEqual(parsed["tls"]["certs"], str(self.root / "tls" / "server.crt"))
        self.assertEqual(parsed["well_known"]["server"], "agents.example:8448")
        self.assertEqual(parsed["well_known"]["client"], "https://agents.example:8448")

    def test_trusted_roots_render_under_tls_in_either_mode(self):
        for mode in bundle.MODES:
            with self.subTest(mode=mode):
                extra = {"address": "0.0.0.0"} if mode == "federated" else {}
                build = proxied if mode == "proxied" else (lambda **kw: meta(mode=mode, **kw))
                text = bundle.deployment_text(build(trusted_roots=["peer.pem"], **extra), self.root)
                parsed = tomllib.loads(text)["global"]
                self.assertEqual(parsed["tls"]["trusted_roots"], [str(self.root / "tls" / "trusted" / "peer.pem")])
                self.assertEqual("certs" in parsed["tls"], mode == "federated")

    def test_fixture_settings_are_absent_unless_asked(self):
        plain = tomllib.loads(bundle.deployment_text(meta(), self.root))["global"]
        self.assertNotIn("ip_range_denylist", plain)
        fixture = tomllib.loads(bundle.deployment_text(meta(mode="federated", address="127.0.0.1", fixture=True),
                                                       self.root))["global"]
        denied = [ipaddress.ip_network(n) for n in fixture["ip_range_denylist"]]
        self.assertTrue(all(ipaddress.ip_address("127.0.0.1") not in n for n in denied))
        for other in ("127.0.0.2", "10.0.0.1", "192.168.1.1", "8.8.8.8", "::1", "fd00::1"):
            self.assertTrue(any(ipaddress.ip_address(other) in n for n in denied), other)
        self.assertEqual(fixture["sender_retry_grace"], 2)

    def test_deployment_file_proxied_delegates_to_the_public_host_on_443(self):
        parsed = tomllib.loads(bundle.deployment_text(proxied(), self.root))["global"]
        self.assertEqual(parsed["address"], "127.0.0.1")
        self.assertEqual(parsed["well_known"]["server"], "matrix.agents.example:443")
        self.assertEqual(parsed["well_known"]["client"], "https://matrix.agents.example")
        self.assertNotIn("ip_range_denylist", parsed, "no peer network, so the server default applies")
        self.assertNotIn("tls", parsed)

    def test_peer_networks_open_exactly_their_range(self):
        text = bundle.deployment_text(proxied(peer_networks=["100.64.0.0/10", "192.168.2.0/24"]), self.root)
        denied = [ipaddress.ip_network(n) for n in tomllib.loads(text)["global"]["ip_range_denylist"]]
        def blocked(address):
            return any(ipaddress.ip_address(address) in n for n in denied)
        for allowed in ("100.64.0.1", "100.127.255.254", "192.168.2.200"):
            self.assertFalse(blocked(allowed), allowed)
        for still in ("192.168.1.35", "10.0.0.1", "172.16.0.1", "127.0.0.1", "169.254.169.254", "::1", "fd00::1"):
            self.assertTrue(blocked(still), still)
        self.assertFalse(blocked("8.8.8.8"), "public addresses were never denied")

    def test_overlapping_peer_networks_open_their_union_in_either_order(self):
        narrow, broad = "192.168.2.0/24", "192.168.0.0/16"
        expected = bundle.denylist_without([broad])
        self.assertEqual(bundle.denylist_without([narrow, broad]), expected)
        self.assertEqual(bundle.denylist_without([broad, narrow]), expected)
        self.assertNotIn(broad, expected)

    def test_deployment_file_keeps_an_explicit_port_in_the_name(self):
        text = bundle.deployment_text(meta(mode="federated", server_name="agents.example:9448", port=8448), self.root)
        parsed = tomllib.loads(text)["global"]
        self.assertEqual(parsed["well_known"]["server"], "agents.example:9448")

    def test_appservice_file_anchors_the_namespace_to_the_server(self):
        text = bundle.appservice_text(meta(server_name="a.b.example"), "AS", "HS")
        parsed = tomllib.loads(text)["global"]["appservice"]["terva"]
        self.assertEqual(parsed["as_token"], "AS")
        self.assertTrue(parsed["device_management"])
        regex = re.compile(parsed["users"][0]["regex"])
        self.assertTrue(regex.match("@terva_alpha:a.b.example"))
        self.assertFalse(regex.match("@terva_alpha:axb.example"))
        self.assertFalse(regex.match("@other:a.b.example"))
        self.assertFalse(regex.match("x@terva_alpha:a.b.example"))

    def test_command_lists_the_three_files_in_override_order(self):
        argv = bundle.command(self.root, Path("/opt/tuwunel"))
        self.assertEqual(argv[0], "/opt/tuwunel")
        self.assertEqual(argv[1::2], ["-c", "-c", "-c"])
        self.assertEqual([Path(p).name for p in argv[2::2]], list(bundle.FILES))


class ValidationTests(unittest.TestCase):
    def test_accepts_a_sound_meta(self):
        bundle.validate_meta(meta())
        bundle.validate_meta(meta(mode="federated", address="0.0.0.0", server_name="agents.example:8448"))

    def test_proxied_mode_rules(self):
        bundle.validate_meta(proxied(peer_networks=["100.64.0.0/10"]))
        for bad in (proxied(address="0.0.0.0"), proxied(public_host="Matrix.Example"),
                    proxied(public_host="matrix.example:8448"), proxied(public_host=None),
                    proxied(peer_networks=["0.0.0.0/0"]), proxied(peer_networks=["127.0.0.0/8"]),
                    proxied(peer_networks=["169.254.0.0/16"]), proxied(peer_networks=["8.8.8.0/24"]),
                    meta(public_host="matrix.agents.example"), meta(peer_networks=["10.0.0.0/8"])):
            with self.subTest(meta=bad), self.assertRaises(ValueError):
                bundle.validate_meta(bad)

    def test_local_mode_refuses_a_public_address(self):
        with self.assertRaises(ValueError):
            bundle.validate_meta(meta(address="0.0.0.0"))

    def test_server_name_must_be_a_lowercase_hostname(self):
        for bad in ("", "Agents.Example", "agents example", "agents.example:99999", "-x.example", "http://x", "agents.example\n"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                bundle.validate_meta(meta(server_name=bad))
        with self.assertRaises(ValueError):
            bundle.validate_meta(proxied(public_host="matrix.agents.example\n"))

    def test_trusted_root_names_are_plain_pem_files(self):
        bundle.validate_meta(meta(trusted_roots=["fleet-ca.pem"]))
        for bad in ("../x.pem", "ca.key", "a b.pem"):
            with self.subTest(name=bad), self.assertRaises(ValueError):
                bundle.validate_meta(meta(trusted_roots=[bad]))

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_ca_issue_publishes_a_matching_pair_and_no_leftovers(self):
        with tempfile.TemporaryDirectory() as temp:
            cadir, out = Path(temp) / "ca", Path(temp) / "tls"
            out.mkdir()
            bundle.ca_init(cadir, "Test CA")
            first = bundle.ca_issue(cadir, "a.example", out)
            serial = bundle.cert_serial(first)
            bundle.ca_issue(cadir, "a.example", out)
            self.assertNotEqual(bundle.cert_serial(out / "server.crt"), serial)
            self.assertEqual(sorted(p.name for p in out.iterdir()), ["server.crt", "server.key"])
            self.assertEqual(oct((out / "server.key").stat().st_mode & 0o777), "0o600")
            pub_key = bundle.run("openssl", "pkey", "-in", str(out / "server.key"), "-pubout")
            pub_crt = bundle.run("openssl", "x509", "-in", str(out / "server.crt"), "-noout", "-pubkey")
            self.assertEqual(pub_key, pub_crt)

    def test_days_until_reads_openssl_dates_as_utc(self):
        # 2027-01-02T03:04:05Z is 1798859045 (calendar.timegm); a local-time
        # reading would be off by the host's zone offset.
        now = 1798859045 - 86400 * 10
        self.assertAlmostEqual(bundle.days_until("Jan  2 03:04:05 2027 GMT", now=now), 10.0)
        self.assertLess(bundle.days_until("Jan  2 03:04:05 2027 GMT", now=1798859045 + 3600), 0)

    def test_state_directory_must_be_outside_git(self):
        with self.assertRaises(ValueError):
            bundle.safe_root(HERE / "state")

    def test_init_refuses_tls_flags_in_local_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "tuwunel"
            fake.write_text("#!/bin/sh\necho tuwunel 0.0.0\n")
            fake.chmod(0o700)
            outside = Path(temp) / "state"
            code = bundle.main(["init", str(outside), "--binary", str(fake), "--server-name", "x.example", "--test-ca"])
            self.assertEqual(code, 2)
            self.assertFalse(outside.exists())

    def test_init_proxied_needs_a_public_host_and_refuses_tls_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = Path(temp) / "tuwunel"
            fake.write_text("#!/bin/sh\necho tuwunel 0.0.0\n")
            fake.chmod(0o700)
            outside = Path(temp) / "state"
            base = ["init", str(outside), "--binary", str(fake), "--server-name", "x.example", "--mode", "proxied"]
            self.assertEqual(bundle.main(base), 2)
            self.assertEqual(bundle.main(base + ["--public-host", "m.x.example", "--test-ca"]), 2)
            self.assertEqual(bundle.main(["init", str(outside), "--binary", str(fake), "--server-name", "x.example",
                                          "--peer-network", "10.0.0.0/8"]), 2)
            self.assertFalse(outside.exists())


class ProbeTests(unittest.TestCase):
    KEY = "Qm9ra3JLZXlNYXRlcmlhbEZvclRlc3RpbmdPbmx5ISE"  # unpadded base64 of 32 bytes
    def run_probe(self, responses, name="agents.example", urls=None):
        def fake(url, context):
            if urls is not None:
                urls.append(url)
            return next((r for suffix, r in responses.items() if url.endswith(suffix)), (404, {}))
        args = types.SimpleNamespace(server_name=name, cafile=None)
        with mock.patch.object(bundle, "fetch", fake), contextlib.redirect_stdout(io.StringIO()):
            return bundle.probe(args)

    def test_malformed_documents_fail_without_crashing(self):
        for shapes in (
            {"/matrix/server": (200, []), "/matrix/client": (200, {"m.homeserver": None})},
            {"/matrix/server": (200, {"m.server": 7}), "/matrix/client": (200, {"m.homeserver": {"base_url": []}}),
             "/version": (200, {"server": "x"}), "/key/v2/server": (200, "text")},
            {"/matrix/server": (200, {"m.server": "matrix.agents.example:notaport"}),
             "/matrix/client": (200, {"m.homeserver": {"base_url": "https://matrix.agents.example:notaport"}})},
        ):
            with self.subTest(shapes=shapes):
                self.assertEqual(self.run_probe(shapes), 1)

    def test_well_formed_documents_pass(self):
        self.assertEqual(self.run_probe({
            "/matrix/server": (200, {"m.server": "matrix.agents.example:443"}),
            "/matrix/client": (200, {"m.homeserver": {"base_url": "https://matrix.agents.example"}}),
            "/client/versions": (200, {"versions": ["v1.11"]}),
            "/federation/v1/version": (200, {"server": {"name": "tuwunel", "version": "1"}}),
            "/key/v2/server": (200, self.key_response("agents.example")),
        }), 0)

    def key_response(self, name, signed=True, valid_until_ts=None):
        if valid_until_ts is None:
            valid_until_ts = int(time.time() * 1000) + 86_400_000
        return {"server_name": name, "valid_until_ts": valid_until_ts, "verify_keys": {"ed25519:a": {"key": self.KEY}},
                "signatures": {name: {"ed25519:a": "c2ln"}} if signed else {}}

    def test_unsigned_or_expired_key_response_fails(self):
        for response in (self.key_response("agents.example", signed=False),
                         self.key_response("agents.example", valid_until_ts=1)):
            with self.subTest(response=response):
                self.assertEqual(self.run_probe({
                    "/matrix/server": (200, {"m.server": "matrix.agents.example:443"}),
                    "/matrix/client": (200, {"m.homeserver": {"base_url": "https://matrix.agents.example"}}),
                    "/client/versions": (200, {"versions": ["v1.11"]}),
                    "/federation/v1/version": (200, {"server": {"name": "tuwunel", "version": "1"}}),
                    "/key/v2/server": (200, response),
                }), 1)

    def test_server_key_needs_usable_key_material(self):
        self.assertEqual(bundle.usable_ed25519_keys({"ed25519:a": {"key": self.KEY}}), ["ed25519:a"])
        for bad in ({}, {"ed25519:a": {}}, {"ed25519:a": {"key": "short"}}, {"ed25519:a": {"key": self.KEY + "="}},
                    {"curve25519:a": {"key": self.KEY}}, {"ed25519:a": {"key": "!" * 43}}, None, []):
            with self.subTest(verify_keys=bad):
                self.assertEqual(bundle.usable_ed25519_keys(bad), [])

    def test_fetch_stops_an_oversized_response(self):
        class Response:
            status = 200
            def read1(self, n):
                return b"x" * n
        class Connection:
            def __init__(self, *args, **kwargs): pass
            def request(self, *args): pass
            def getresponse(self): return Response()
            def close(self): pass
        with mock.patch.object(bundle.http.client, "HTTPSConnection", Connection), \
                self.assertRaises(bundle.http.client.HTTPException):
            bundle.fetch("https://agents.example/x", None)

    def test_portless_delegation_uses_the_federation_port(self):
        urls = []
        self.run_probe({"/matrix/server": (200, {"m.server": "matrix.agents.example"})}, urls=urls)
        self.assertIn("https://matrix.agents.example:8448/_matrix/federation/v1/version", urls)
        self.assertIn("https://matrix.agents.example:8448/_matrix/key/v2/server", urls)

    def test_explicit_port_skips_delegation_and_uses_the_port(self):
        urls = []
        self.assertEqual(self.run_probe({
            "/client/versions": (200, {"versions": ["v1.11"]}),
            "/federation/v1/version": (200, {"server": {"name": "tuwunel", "version": "1"}}),
            "/key/v2/server": (200, self.key_response("agents.example:9448")),
        }, name="agents.example:9448", urls=urls), 1)  # no client well-known, so one check fails
        self.assertNotIn("https://agents.example/.well-known/matrix/server", urls)
        self.assertIn("https://agents.example/.well-known/matrix/client", urls)
        self.assertIn("https://agents.example:9448/_matrix/federation/v1/version", urls)
        self.assertIn("https://agents.example:9448/_matrix/key/v2/server", urls)


class IdpTests(unittest.TestCase):
    ISSUER = "https://auth.agents.example/application/o/agents-matrix/"
    SECRET = "not-a-real-secret-0123456789"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        fake = self.temp / "tuwunel"
        fake.write_text("#!/bin/sh\necho tuwunel 0.0.0\n")
        fake.chmod(0o700)
        self.fake = fake
        self.root = self.temp / "state"
        self.quiet(["init", str(self.root), "--binary", str(fake), "--server-name", "agents.example",
                    "--mode", "proxied", "--public-host", "matrix.agents.example"])
        self.secret = self.root / "config" / "oidc-client-secret"
        self.secret.touch(mode=0o600)
        self.secret.write_text(self.SECRET + "\n")

    def quiet(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = bundle.main(argv)
        return code, out.getvalue()

    def add(self, *extra, client_id="agents-matrix", issuer=None):
        return self.quiet(["idp", str(self.root), "--issuer-url", issuer or self.ISSUER, "--client-id", client_id, *extra])

    def test_writes_a_fourth_file_with_the_secret_by_reference(self):
        code, out = self.add()
        self.assertEqual(code, 0)
        self.assertNotIn(self.SECRET, out)
        text = (self.root / "config" / bundle.IDP_FILE).read_text()
        self.assertNotIn(self.SECRET, text)
        [provider] = tomllib.loads(text)["global"]["identity_provider"]
        self.assertEqual(provider["callback_url"],
                         "https://matrix.agents.example/_matrix/client/unstable/login/sso/callback/agents-matrix")
        self.assertEqual(provider["client_secret_file"], str(self.secret))
        self.assertEqual((provider["trusted"], provider["unique_id_fallbacks"], provider["registration"]),
                         (False, False, True))
        self.assertEqual(provider["userid_claims"], ["preferred_username"])
        self.assertEqual((self.root / "config" / bundle.IDP_FILE).stat().st_mode & 0o777, 0o600)
        root, meta, binary = bundle.load(self.root)
        self.assertEqual(bundle.command(root, binary).count("-c"), 4)
        self.assertEqual(meta["identity_provider"]["client_id"], "agents-matrix")

    def test_client_id_and_issuer_are_permanent(self):
        self.assertEqual(self.add()[0], 0)
        self.assertEqual(self.add(client_id="other")[0], 2)
        self.assertEqual(self.add(issuer="https://auth.agents.example/application/o/other/")[0], 2)
        self.assertEqual(self.add("--no-registration")[0], 0)
        self.assertFalse(bundle.load(self.root)[1]["identity_provider"]["registration"])

    def test_refuses_an_exposed_empty_or_missing_secret(self):
        self.secret.chmod(0o644)
        self.assertEqual(self.add()[0], 2)
        self.secret.chmod(0o600)
        self.secret.write_text("  \n")
        self.assertEqual(self.add()[0], 2)
        self.assertEqual(self.add("--secret-file", str(self.temp / "absent"))[0], 2)
        self.assertFalse((self.root / "config" / bundle.IDP_FILE).exists())

    def test_refuses_bad_inputs(self):
        for client_id, issuer in (("has space", None), ("ok", "http://auth.agents.example/x/"),
                                  ("ok", "https://Auth.example/x/"), ("ok", 'https://auth.example/"x')):
            with self.subTest(client_id=client_id, issuer=issuer):
                self.assertEqual(self.add(client_id=client_id, issuer=issuer)[0], 2)

    def test_accepts_an_issuer_with_or_without_a_path(self):
        for issuer in ("https://auth.agents.example", "https://auth.agents.example:8443/realms/x"):
            with self.subTest(issuer=issuer):
                fresh = self.temp / f"state-{len(issuer)}"
                self.quiet(["init", str(fresh), "--binary", str(self.fake), "--server-name", "agents.example",
                            "--mode", "proxied", "--public-host", "matrix.agents.example"])
                secret = fresh / "config" / "oidc-client-secret"
                secret.touch(mode=0o600)
                secret.write_text(self.SECRET)
                code, _ = self.quiet(["idp", str(fresh), "--issuer-url", issuer, "--client-id", "agents-matrix"])
                self.assertEqual(code, 0)

    def test_needs_proxied_mode(self):
        local = self.temp / "local"
        self.quiet(["init", str(local), "--binary", str(self.fake), "--server-name", "agents.example"])
        (local / "config" / "oidc-client-secret").touch(mode=0o600)
        (local / "config" / "oidc-client-secret").write_text(self.SECRET)
        code, _ = self.quiet(["idp", str(local), "--issuer-url", self.ISSUER, "--client-id", "agents-matrix"])
        self.assertEqual(code, 2)

    def test_load_refuses_a_file_without_its_record_and_idp_repairs_it(self):
        self.assertEqual(self.add()[0], 0)
        meta = json.loads((self.root / "bundle.json").read_text())
        del meta["identity_provider"]
        (self.root / "bundle.json").write_text(json.dumps(meta))
        with self.assertRaises(ValueError):
            bundle.load(self.root)
        self.assertEqual(self.add()[0], 0, "rerunning idp repairs the disagreement")
        self.assertEqual(bundle.load(self.root)[1]["identity_provider"]["client_id"], "agents-matrix")


if __name__ == "__main__":
    unittest.main()
