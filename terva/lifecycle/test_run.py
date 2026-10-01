"""Offline tests for the lifecycle runner. No Docker daemon required."""

from contextlib import redirect_stdout
import io
import ipaddress
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run as lifecycle


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_denylist_allows_exactly_the_two_peer_addresses(self):
        addresses = ["172.30.0.10", "172.30.0.11"]
        denied = [ipaddress.ip_network(n) for n in lifecycle.deny_except(addresses)]
        def blocked(address):
            ip = ipaddress.ip_address(address)
            return any(ip in network for network in denied if network.version == ip.version)
        for address in addresses:
            self.assertFalse(blocked(address))
        for address in ("172.30.0.9", "172.30.0.12", "127.0.0.1", "10.0.0.1", "169.254.169.254",
                        "8.8.8.8", "255.255.255.255", "0.0.0.0", "::1", "2001:4860:4860::8888"):
            self.assertTrue(blocked(address), address)
        ipv4 = [n for n in denied if n.version == 4]
        self.assertEqual(sum(n.num_addresses for n in ipv4), 2**32 - 2)
        self.assertEqual(len(list(ipaddress.collapse_addresses(ipv4))), len(ipv4))

    def test_denylist_without_peers_denies_everything(self):
        self.assertEqual(lifecycle.deny_except([]), ["0.0.0.0/0", "::/0"])

    def test_denylist_rejects_non_ipv4_peers(self):
        for address in ("example.test", "::1", "10.0.0.0/24"):
            with self.assertRaises(ValueError):
                lifecycle.deny_except([address])

    def test_deadline_reports_timeout_not_permanent_failure(self):
        with patch.object(lifecycle.time, "monotonic", side_effect=[0, 11]), \
                patch.object(lifecycle.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "not proof of permanent loss"):
                lifecycle.wait_for(lambda: False, 10, "delivery")

    def test_transient_timeout_is_retried_within_deadline(self):
        with patch.object(lifecycle.time, "monotonic", return_value=0), patch.object(lifecycle.time, "sleep"):
            checks = iter([TimeoutError(), True])
            def check():
                value = next(checks)
                if isinstance(value, Exception):
                    raise value
                return value
            lifecycle.wait_for(check, 10, "recovery")

    def test_assertion_errors_are_not_retried(self):
        def check():
            raise ValueError("permanent HTTP assertion")
        with self.assertRaisesRegex(ValueError, "permanent HTTP"):
            lifecycle.wait_for(check, 10, "delivery")

    def test_failed_assertion_is_written_before_raising(self):
        fixture = lifecycle.Fixture(self.root, 10)
        with self.assertRaisesRegex(ValueError, "Assertion failed"):
            fixture.record("deliberate failure", False)
        report = json.loads((self.root / "results.json").read_text())
        self.assertEqual(report["assertions"], [{"name": "deliberate failure", "passed": False}])

    def test_atomic_report_is_private(self):
        lifecycle.save(self.root / "report.json", {"status": "running"})
        lifecycle.save(self.root / "report.json", {"status": "passed"})
        self.assertEqual(json.loads((self.root / "report.json").read_text())["status"], "passed")
        self.assertEqual((self.root / "report.json").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / "report.json.tmp").exists())

    def test_ownership_mismatch_refuses_mutation(self):
        with self.assertRaisesRegex(ValueError, "another run"):
            lifecycle.owned({"Config": {"Labels": {lifecycle.LABEL: "other"}}}, "ours")
        lifecycle.owned({"Labels": {lifecycle.LABEL: "ours"}}, "ours")

    def owner(self):
        state = {"run_id": "tuwunel-life-123456789abc", "containers": ["our-server"], "networks": ["our-network"]}
        lifecycle.save(self.root / "owner.json", state)
        return state

    def test_cleanup_checks_labels_before_stop_or_remove(self):
        self.owner()
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if "ls" in args:
                return "container-id\n"
            return json.dumps([{"Name": "/our-server", "Config": {"Labels": {lifecycle.LABEL: "someone-else"}}}])
        with patch.object(lifecycle, "command", side_effect=command):
            with self.assertRaisesRegex(ValueError, "another run"):
                lifecycle.cleanup(self.root)
        self.assertFalse(any("stop" in c or "rm" in c for c in calls))

    def test_cleanup_rejects_unexpected_names_even_with_our_label(self):
        state = self.owner()
        with patch.object(lifecycle, "command", side_effect=["id", json.dumps([
            {"Name": "/unexpected", "Config": {"Labels": {lifecycle.LABEL: state["run_id"]}}}])]) as command:
            with self.assertRaisesRegex(ValueError, "Unexpected resource"):
                lifecycle.cleanup(self.root)
        self.assertEqual(command.call_count, 2)

    def test_cleanup_uses_ids_and_never_deletes_data(self):
        state = self.owner()
        sentinel = self.root / "data"
        sentinel.write_text("retain me")
        values = ["cid", json.dumps([{"Name": "/our-server", "Config": {"Labels": {lifecycle.LABEL: state["run_id"]}}}]),
                  "", "", "", "nid", json.dumps([{"Name": "our-network", "Labels": {lifecycle.LABEL: state["run_id"]}}]), "", ""]
        with patch.object(lifecycle, "command", side_effect=values) as command:
            lifecycle.cleanup(self.root)
        self.assertIn((("docker", "container", "rm", "cid"),), [(call.args,) for call in command.call_args_list])
        self.assertTrue(sentinel.exists())
        self.assertFalse(any("prune" in call.args for call in command.call_args_list))

    def test_delivery_uses_sync_and_advances_cursor_only_after_success(self):
        fixture = lifecycle.Fixture(self.root, 10)
        responses = [TimeoutError(), {"next_batch": "next", "rooms": {}},
                     {"next_batch": "final", "rooms": {"join": {"room": {"timeline": {"events": [{"event_id": "event"}]}}}}}]
        with patch.object(fixture, "sync", side_effect=responses) as sync, \
                patch.object(lifecycle.time, "sleep"), redirect_stdout(io.StringIO()):
            fixture.delivered({}, "room", "event", "original")
        self.assertEqual([call.args[1] for call in sync.call_args_list], ["original", "original", "next"])

    def test_limited_timeline_does_not_claim_delivery(self):
        fixture = lifecycle.Fixture(self.root, 10)
        with patch.object(fixture, "sync", return_value={"next_batch": "next", "rooms": {"join": {
                "room": {"timeline": {"limited": True, "events": [{"event_id": "event"}]}}}}}):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                fixture.delivered({}, "room", "event", "original")


if __name__ == "__main__":
    unittest.main()
