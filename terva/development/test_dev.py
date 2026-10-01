"""Offline tests for configuration and snapshot guards, without a Docker daemon."""

from contextlib import nullcontext
import os
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import dev


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "instance"
        self.root.mkdir(mode=0o700)
        self.meta = {"format": 1, "host": "unit.test", "server_name": "unit.test:8448", "port": 18448,
                     "image": "sha256:" + "a" * 64, "relay_image": "sha256:" + "b" * 64,
                     "uid": os.getuid(), "gid": os.getgid()}
        for name in ("config", "tls", "data"):
            (self.root / name).mkdir(mode=0o700)
        (self.root / "config/tuwunel.toml").write_text(dev.config_text(self.meta, {"as_token": "fake-as", "hs_token": "fake-hs"}))
        (self.root / "tls/ca.crt").write_text("fake certificate")
        (self.root / "data/media").mkdir()
        (self.root / "data/media/example").write_bytes(b"media contents\x00")
        (self.root / "compose.yaml").write_bytes((dev.HERE / "compose.yaml").read_bytes())
        dev.put_json(self.root / "credentials.json", {"as_token": "fake-as"})
        self.meta["assets"] = {"compose.yaml": dev.digest(self.root / "compose.yaml"),
                               "credentials.json": dev.digest(self.root / "credentials.json")}
        for folder in ("config", "tls"):
            self.meta["assets"].update({f"{folder}/{k}": v for k, v in dev.inventory(self.root / folder).items()})
        dev.put_json(self.root / "instance.json", self.meta)
        self.clean = {"State": {"Status": "exited", "ExitCode": 0, "OOMKilled": False}}

    def backup(self):
        target = self.base / "snapshot"
        with patch.object(dev, "container", return_value=self.clean):
            dev.snapshot(self.root, self.meta, target)
        return target

    def test_config_is_safe_and_namespace_is_local(self):
        config = tomllib.loads((self.root / "config/tuwunel.toml").read_text())["global"]
        for key in ("allow_registration", "allow_guest_registration", "grant_admin_to_first_user", "allow_invalid_tls_certificates"):
            self.assertFalse(config[key])
        self.assertNotIn("ip_range_denylist", config)
        self.assertFalse(config["tls"]["dual_protocol"])
        registration = config["appservice"]["terva"]
        self.assertTrue(registration["device_management"])
        self.assertNotIn("url", registration)
        regex = registration["users"][0]["regex"]
        self.assertRegex("@terva_agent:unit.test:8448", regex)
        for invalid in ("@terva_agent:unitXtest:8448", "@terva_agent:other.test:8448", "@other:unit.test:8448"):
            self.assertIsNone(re.search(regex, invalid))

    def test_meta_rejects_unsafe_hosts_ports_and_images(self):
        for field, values in {"host": ["example.com", "a/b.test", "a\n.test"],
                              "port": [0, 1023, 65536, True], "image": ["latest", "sha256:x"]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    dev.validate_meta(dict(self.meta, **{field: value}))

    def test_load_refuses_changed_assets(self):
        self.assertEqual(dev.load(self.root), self.meta)
        with (self.root / "config/tuwunel.toml").open("a") as stream:
            stream.write("\n# changed\n")
        with self.assertRaisesRegex(ValueError, "Launch assets changed"):
            dev.load(self.root)

    def test_load_refuses_symlink_metadata(self):
        file = self.root / "instance.json"
        file.rename(self.base / "elsewhere.json")
        file.symlink_to(self.base / "elsewhere.json")
        with self.assertRaisesRegex(ValueError, "Invalid instance file"):
            dev.load(self.root)

    def test_paths_cannot_be_inside_git(self):
        with self.assertRaisesRegex(ValueError, "outside Git"):
            dev.safe_path(dev.HERE / "never-create")

    def test_paths_cannot_follow_symlink_parents(self):
        link = self.base / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            dev.safe_path(link / "new")

    def test_new_directory_never_overwrites(self):
        with self.assertRaisesRegex(ValueError, "already exists"):
            dev.new_directory(self.root)

    def test_only_zero_exit_without_oom_counts_as_clean(self):
        dev.stopped(self.clean)
        for state in (dict(Status="running", ExitCode=0, OOMKilled=False),
                      dict(Status="exited", ExitCode=137, OOMKilled=False),
                      dict(Status="exited", ExitCode=0, OOMKilled=True)):
            with self.assertRaises(ValueError):
                dev.stopped({"State": state})
        with self.assertRaises(ValueError):
            dev.stopped(None)

    def test_running_backup_is_refused_before_destination_creation(self):
        target = self.base / "snapshot"
        with patch.object(dev, "container", return_value={"State": {"Status": "running"}}):
            with self.assertRaises(ValueError):
                dev.snapshot(self.root, self.meta, target)
        self.assertFalse(target.exists())

    def test_symlink_and_fifo_snapshots_are_refused(self):
        link = self.root / "data/link"
        link.symlink_to(self.base / "elsewhere")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.backup()
        link.unlink()
        os.mkfifo(link)
        with self.assertRaisesRegex(ValueError, "special file"):
            self.backup()

    def test_nested_snapshot_is_refused(self):
        with self.assertRaisesRegex(ValueError, "separate"):
            dev.snapshot(self.root, self.meta, self.root / "backup")

    def test_restore_copies_media_and_retains_original(self):
        source = self.backup()
        target = self.base / "restored"
        with patch.object(dev, "container", return_value=None), patch.object(dev, "run"), \
                patch.object(dev, "locked", return_value=nullcontext()):
            dev.restore(source, target)
        self.assertEqual(dev.inventory(target), dev.inventory(self.root))
        self.assertEqual((target / "data/media/example").read_bytes(), b"media contents\x00")
        self.assertTrue((source / "snapshot.json").exists())

    def test_restore_refuses_corrupt_missing_and_extra_files(self):
        source = self.backup()
        media = source / "data/media/example"
        original = media.read_bytes()
        for change in ("corrupt", "missing", "extra"):
            with self.subTest(change=change):
                if change == "corrupt":
                    media.write_bytes(b"corrupted")
                elif change == "missing":
                    media.unlink()
                else:
                    (source / "extra").write_text("unexpected")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    dev.restore(source, self.base / "restored")
                self.assertFalse((self.base / "restored").exists())
                media.write_bytes(original)

    def test_restore_refuses_duplicate_identity(self):
        source = self.backup()
        with patch.object(dev, "container", return_value=self.clean), \
                patch.object(dev, "locked", return_value=nullcontext()):
            with self.assertRaisesRegex(ValueError, "original identity"):
                dev.restore(source, self.base / "restored")
        self.assertFalse((self.base / "restored").exists())

    def test_compose_ignores_environment_overrides(self):
        with patch.dict(os.environ, {"COMPOSE_FILE": "hostile.yaml", "TUWUNEL_DEV_PORT": "80"}), \
                patch.object(dev.subprocess, "run") as run:
            run.return_value.returncode = 0
            dev.compose(self.root, self.meta, "up", "-d")
        args, kwargs = run.call_args
        self.assertNotIn("COMPOSE_FILE", kwargs["env"])
        self.assertEqual(kwargs["env"]["TUWUNEL_DEV_PORT"], "18448")
        self.assertIn(os.devnull, args[0])


if __name__ == "__main__":
    unittest.main()
