#!/usr/bin/env python3
"""Live loopback/concurrency check. All three runs retain private artifacts."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import loopback


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--tester", required=True)
    parser.add_argument("--testee", required=True)
    args = parser.parse_args()
    root = Path(args.directory).expanduser().resolve()
    loopback.require(not root.is_relative_to(loopback.ROOT) and not root.exists(), "Use a new private directory outside Git")
    root.mkdir(mode=0o700)
    processes = []
    report = {"status": "failed"}
    runner = str(Path(loopback.__file__))
    tester, testee = loopback.image(args.tester, True), loopback.image(args.testee)

    def start(name, *extra):
        path = root / name
        stream = (root / (name + ".log")).open("wb")
        process = subprocess.Popen([sys.executable, "-B", runner, "run", str(path), "--tester", tester,
                                    "--testee", testee, *extra], stdout=stream, stderr=subprocess.STDOUT)
        stream.close()
        processes.append((process, path))
        return process, path

    def wait(process, path, expected):
        code = process.wait(timeout=240)
        loopback.require(code == expected, "Unexpected exit for " + path.name)
        result = json.loads((path / "report.json").read_text())
        loopback.require(result["cleanup"] == "passed", "Cleanup failed for " + path.name)
        return result

    try:
        held, fixture = start("held", "--probe", "--hold-seconds", "120")
        for _ in range(180):
            if (fixture / "ready").exists():
                break
            loopback.require(held.poll() is None, "Held fixture exited before readiness")
            time.sleep(1)
        else:
            raise RuntimeError("Held fixture did not become ready")
        owner = json.loads((fixture / "owner.json").read_text())
        token = owner["run_id"]
        evidence = loopback.bindings(fixture / "bindings.jsonl", token)
        loopback.require({e["phase"] for e in evidence} == {"base", "blueprint"}, "Missing base or blueprint evidence")
        before = {kind: set(loopback.ids(kind, token)) for kind in ("container", "network", "image")}
        loopback.require(all(before.values()), "Held fixture lacks owned resources")
        running = {rid: json.loads(loopback.docker("inspect", rid))[0]["State"]["Running"] for rid in before["container"]}
        loopback.require(any(running.values()), "Held fixture has no running deployment")

        def intact():
            loopback.require(held.poll() is None, "Held fixture exited too early")
            for kind, expected in before.items():
                loopback.require(set(loopback.ids(kind, token)) == expected, "Another run changed the held " + kind + " set")
            for rid in before["container"]:
                item = json.loads(loopback.docker("inspect", rid))[0]
                loopback.require(item["State"]["Running"] == running[rid], "Another run changed the held container state")
                if not running[rid]:
                    continue  # Blueprint construction leaves a stopped source container.
                port = item["NetworkSettings"]["Ports"]["8008/tcp"][0]["HostPort"]
                with urllib.request.urlopen("http://127.0.0.1:" + port + "/_matrix/client/versions", timeout=5) as response:
                    loopback.require(response.status == 200, "Held homeserver is not reachable")
            for image in (tester, testee):
                loopback.require(loopback.image(image) == image, "A base image disappeared")

        intact()
        smoke, smoke_path = start("smoke")
        report["smoke"] = wait(smoke, smoke_path, 0)
        intact()
        rejected, rejected_path = start("rejected", "--probe", "--reject")
        report["rejected"] = wait(rejected, rejected_path, 1)
        rejection = json.loads((rejected_path / "rejection.json").read_text())
        loopback.require(rejection == {"rejected": True, "containers_remaining": 0}, "Rejection did not remove its newly created container")
        intact()
        report["concurrent_resources_unchanged"] = {kind: len(values) for kind, values in before.items()}
        report["held"] = wait(held, fixture, 0)
        for process, path in processes:
            loopback.cleanup(path)
            loopback.cleanup(path)
        report["idempotent_cleanup"] = "passed"
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        for process, path in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=90)
            if (path / "owner.json").exists():
                try:
                    loopback.cleanup(path)
                except Exception as exc:
                    report.update(status="failed", cleanup_error=str(exc))
        loopback.save(root / "verification.json", report)
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
