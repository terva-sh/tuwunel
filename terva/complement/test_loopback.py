"""Offline guards for the scoped Complement runner."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import loopback


class LoopbackTests(unittest.TestCase):
    def test_complete_results(self):
        raw = '\n'.join(json.dumps(e) for e in [{"Action":"pass", "Test":"TestOne"}, {"Action":"pass"}])
        self.assertEqual(loopback.results(raw), {"TestOne":"pass"})

    def test_failed_duplicate_stays_failed(self):
        raw = '\n'.join(json.dumps(e) for e in [{"Action":"fail", "Test":"TestOne"}, {"Action":"pass", "Test":"TestOne"}, {"Action":"pass"}])
        with self.assertRaises(RuntimeError):
            loopback.results(raw)

    def test_empty_results_refused(self):
        for raw in ('', '{"Action":"pass"}', '{"Action":"pass","Test":"TestOne"}'):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                loopback.results(raw)

    def test_malformed_results_refused(self):
        with self.assertRaises(ValueError):
            loopback.results('not JSON')

    def binding(self, host="127.0.0.1", port="32100", token="abcdef123456", phase="base"):
        return {"run_id":token, "phase":phase, "ports": {p:[{"HostIp":host,"HostPort":port}] for p in ("8008/tcp","8448/tcp")}}

    def validate(self, entries):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"bindings.jsonl"
            path.write_text('\n'.join(json.dumps(e) for e in entries))
            return loopback.bindings(path, "abcdef123456")

    def test_loopback_bindings(self):
        self.assertEqual(len(self.validate([self.binding()])), 1)

    def test_wildcard_ipv4_and_ipv6_refused(self):
        for host in ("", "0.0.0.0", "::", "::1", "192.0.2.1"):
            with self.subTest(host=host), self.assertRaises(RuntimeError):
                self.validate([self.binding(host)])

    def test_extra_ipv6_binding_refused(self):
        entry = self.binding()
        entry["ports"]["8008/tcp"].append({"HostIp":"::", "HostPort":"32100"})
        with self.assertRaises(RuntimeError):
            self.validate([entry])

    def test_missing_binding_refused(self):
        entry = self.binding()
        del entry["ports"]["8448/tcp"]
        with self.assertRaises(RuntimeError):
            self.validate([entry])

    def test_bad_allocated_ports_refused(self):
        for port in ("", "0", "65536", "bad"):
            with self.subTest(port=port), self.assertRaises(RuntimeError):
                self.validate([self.binding(port=port)])

    def test_wrong_owner_and_phase_refused(self):
        for entry in (self.binding(token="000000000000"), self.binding(phase="unknown")):
            with self.assertRaises(RuntimeError):
                self.validate([entry])

    def test_empty_binding_evidence_refused(self):
        with self.assertRaises(RuntimeError):
            self.validate([])

    def test_unpatched_image_refused(self):
        item = {"Id":"sha256:"+"a"*64,"Config":{"Labels":{},"Entrypoint":["go","test"]}}
        with patch.object(loopback, "docker", return_value=json.dumps([item])), self.assertRaises(RuntimeError):
            loopback.image("unpatched", True)

    def test_unexpected_entrypoint_refused(self):
        item = {"Id":"sha256:"+"a"*64,"Config":{"Labels":{"sh.terva.complement.loopback":"v1"},"Entrypoint":["sh"]}}
        with patch.object(loopback, "docker", return_value=json.dumps([item])), self.assertRaises(RuntimeError):
            loopback.image("wrong-entrypoint", True)

    def test_cleanup_rejects_invalid_owner_before_docker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"owner.json").write_text('{"run_id":""}')
            with patch.object(loopback, "docker") as docker, self.assertRaises(RuntimeError):
                loopback.cleanup(root)
            docker.assert_not_called()

    def test_cleanup_refuses_foreign_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"owner.json").write_text('{"run_id":"abcdef123456"}')
            foreign = {"Id":"a"*64,"Config":{"Labels":{"complement_run_id":"other"}}}
            with patch.object(loopback, "ids", return_value=["a"*64]), patch.object(loopback, "docker", side_effect=["", json.dumps([foreign])]) as docker:
                with self.assertRaises(RuntimeError):
                    loopback.cleanup(root)
                self.assertEqual(docker.call_count, 2)
                self.assertEqual(docker.call_args.args[1], "inspect")

    def test_cleanup_recovers_outer_before_owner_id_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token, cid = "abcdef123456", "a" * 64
            (root / "owner.json").write_text(json.dumps({"run_id": token}))
            item = {"Name": "/terva-complement-" + token,
                    "Config": {"Labels": {"sh.terva.complement.run": token}}}
            found = loopback.subprocess.CompletedProcess([], 0, json.dumps([item]).encode(), b"")
            logs = loopback.subprocess.CompletedProcess([], 0, b"", b"")
            with patch.object(loopback, "ids", return_value=[]), \
                 patch.object(loopback, "docker", return_value=cid) as docker, \
                 patch.object(loopback.subprocess, "run", side_effect=[found, logs]):
                loopback.cleanup(root)
            self.assertEqual(docker.call_args_list[1].args, ("stop", "--time", "10", cid))
            self.assertEqual(docker.call_args_list[2].args, ("rm", cid))
            self.assertEqual(json.loads((root / "cleanup.json").read_text())["status"], "passed")

    def test_cleanup_refuses_ambiguous_outer_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "owner.json").write_text('{"run_id":"abcdef123456"}')
            with patch.object(loopback, "docker", return_value="a" * 64 + "\n" + "b" * 64) as docker:
                with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
                    loopback.cleanup(root)
            self.assertEqual(docker.call_count, 1)


if __name__ == "__main__":
    unittest.main()
