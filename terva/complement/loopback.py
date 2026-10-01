#!/usr/bin/env python3
"""Run the reviewed loopback-only Complement tester. Requires Python 3.11+."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
POLICY = "v1"
SMOKE = "^TestComplemauAppserviceReceivesTyping$"
RUN_ID = re.compile(r"[a-f0-9]{12}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def docker(*args, timeout=60):
    result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout)
    require(result.returncode == 0, "Docker operation failed: " + args[0] + "; inspect private artifacts and Docker state")
    return result.stdout.decode().strip()


def save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def image(ref, tester=False):
    item = json.loads(docker("image", "inspect", ref))[0]
    require(IMAGE_ID.fullmatch(item["Id"]), "Expected immutable local image ID")
    if tester:
        require(item["Config"].get("Labels", {}).get("sh.terva.complement.loopback") == POLICY,
                "Tester lacks the reviewed loopback policy label")
        require(item["Config"].get("Entrypoint") == ["go", "test"], "Unexpected tester entrypoint")
    return item["Id"]


def ids(kind, token):
    args = {"container": ("ps", "-aq"), "network": ("network", "ls", "-q"), "image": ("image", "ls", "-q")}[kind]
    return list(dict.fromkeys(docker(*args, "--filter", "label=complement_run_id=" + token).split()))


def cleanup(root):
    require(not root.resolve().is_relative_to(ROOT), "Cleanup artifacts must be outside the checkout")
    owner = json.loads((root / "owner.json").read_text())
    token = owner["run_id"]
    require(RUN_ID.fullmatch(token), "Invalid run owner")
    outer = owner.get("container_id")
    if not outer:
        # Recover interruption between Docker create and saving its returned ID.
        matches = docker("ps", "-aq", "--no-trunc", "--filter", "label=sh.terva.complement.run=" + token).split()
        require(len(matches) <= 1, "Ambiguous outer container ownership")
        outer = matches[0] if matches else None
    # Stop the producer before enumerating resources so it cannot race cleanup.
    if outer:
        require(re.fullmatch(r"[a-f0-9]{64}", outer), "Invalid outer container ID")
        found = subprocess.run(["docker", "inspect", outer], capture_output=True, timeout=60)
        if found.returncode == 0:
            item = json.loads(found.stdout)[0]
            require(item["Name"] == "/terva-complement-" + token and
                    item["Config"]["Labels"].get("sh.terva.complement.run") == token, "Outer container ownership mismatch")
            docker("stop", "--time", "10", outer)
            logs = subprocess.run(["docker", "logs", outer], capture_output=True, timeout=30)
            (root / "go.jsonl").write_bytes(logs.stdout)
            (root / "tester.log").write_bytes(logs.stderr)
            docker("rm", outer)
    for kind in ("container", "network", "image"):
        for rid in ids(kind, token):
            item = json.loads(docker(kind, "inspect", rid))[0]
            labels = item["Config"]["Labels"] if kind != "network" else item["Labels"]
            require(labels.get("complement_run_id") == token, "Resource ownership mismatch")
            if kind == "container":
                logs = subprocess.run(["docker", "logs", rid], capture_output=True, timeout=30)
                (root / (rid[:12] + ".log")).write_bytes(logs.stdout + logs.stderr)
                docker("rm", "-f", item["Id"])
            elif kind == "network":
                docker("network", "rm", item["Id"])
            else:
                # Remove only this run's immutable blueprint image, never prune or force.
                docker("image", "rm", item["Id"])
        require(not ids(kind, token), "Cleanup left owned " + kind + " resources")
    save(root / "cleanup.json", {"run_id": token, "status": "passed"})


def results(raw):
    outcomes = {}
    package_pass = False
    for line in raw.splitlines():
        event = json.loads(line)
        action = event.get("Action")
        test = event.get("Test")
        if test and action in ("pass", "fail", "skip"):
            old = outcomes.get(test)
            if old != "fail":
                outcomes[test] = action
        if not test and action == "pass":
            package_pass = True
    require(outcomes and package_pass and "fail" not in outcomes.values(), "Go tests failed or produced no complete result set")
    return outcomes


def bindings(path, token):
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    require(entries, "No verified bindings recorded")
    for entry in entries:
        require(entry["run_id"] == token and entry["phase"] in ("base", "blueprint"), "Invalid binding evidence owner or phase")
        ports = entry["ports"]
        require({"8008/tcp", "8448/tcp"} <= ports.keys(), "Missing required bindings")
        for values in ports.values():
            require(len(values) == 1 and values[0]["HostIp"] == "127.0.0.1" and
                    str(values[0]["HostPort"]).isdigit() and 1 <= int(values[0]["HostPort"]) <= 65535,
                    "Unsafe or incomplete published binding evidence")
    return entries


def run(args):
    root = Path(args.directory).expanduser().resolve()
    require(not root.is_relative_to(ROOT), "Run artifacts must be outside the checkout")
    require(root.parent.is_dir() and not root.exists(), "Use a new directory beneath an existing parent")
    require("," not in str(root) and "\n" not in str(root), "Unsupported mount path")
    tester = image(args.tester, True)
    testee = image(args.testee)
    require(0 <= args.hold_seconds <= 300, "Hold duration must be 0..300")
    token = uuid.uuid4().hex[:12]
    require(all(not ids(kind, token) for kind in ("container", "network", "image")), "Run ID already owns resources")
    require(not docker("ps", "-aq", "--filter", "label=sh.terva.complement.run=" + token), "Run ID already has a tester")
    root.mkdir(mode=0o700)
    owner = {"run_id": token, "tester": tester, "testee": testee,
             "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save(root / "owner.json", owner)
    report = {"run_id": token, "status": "failed"}
    started = time.monotonic()
    try:
        package = "./internal/docker" if args.probe else "./tests/complemau/..."
        selection = "^TestTervaLoopbackLive$" if args.probe else args.run
        env = {"COMPLEMENT_BASE_IMAGE": testee, "COMPLEMENT_RUN_ID": token,
               "COMPLEMENT_HOSTNAME_RUNNING_COMPLEMENT": "host.docker.internal",
               "COMPLEMENT_HOST_MOUNTS": "", "COMPLEMENT_POST_TEST_SCRIPT": "",
               "COMPLEMENT_ENABLE_DIRTY_RUNS": "0", "COMPLEMENT_ALWAYS_PRINT_SERVER_LOGS": "0",
               "COMPLEMENT_DEBUG": "0", "COMPLEMENT_TESTEE_CAP_ADD": "SYS_NICE",
               "COMPLEMENT_CONTAINER_CPU_CORES": "2", "COMPLEMENT_CONTAINER_MEMORY": "2GB",
               "COMPLEMENT_SPAWN_HS_TIMEOUT_SECS": "120", "COMPLEMENT_DESTROY_HS_TIMEOUT_SECS": "10",
               "TERVA_LOOPBACK_LIVE": "1" if args.probe else "0",
               "TERVA_LOOPBACK_REJECT": "1" if args.reject else "0",
               "TERVA_LOOPBACK_HOLD_SECONDS": str(args.hold_seconds), "GOMAXPROCS": "2"}
        command = ["create", "--name", "terva-complement-" + token,
                   "--label", "sh.terva.complement.run=" + token, "--network", "host",
                   "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
                   "--mount", "type=bind,src=" + str(root) + ",dst=/terva-output",
                   "--memory", "2g", "--cpus", "2"]
        for key, value in env.items():
            command += ["--env", key + "=" + value]
        command += [tester, "-json", "-count=1", "-parallel=1", "-timeout=30m",
                    "-tags=conduwuit_blacklist,complemau", "-skip=TestComplemauToDeviceBurstFansOut", "-run=" + selection, package]
        save(root / "invocation.json", {"argv": ["docker", *command], "environment": env})
        owner["container_id"] = docker(*command)
        save(root / "owner.json", owner)
        docker("start", owner["container_id"])
        while True:
            state = json.loads(docker("inspect", owner["container_id"]))[0]["State"]
            if not state["Running"]:
                break
            require(time.monotonic() - started < 1860, "Tester deadline expired")
            time.sleep(1)
        raw = docker("logs", owner["container_id"])
        # Always retain per-test outcomes, including expected baseline failures.
        events = [json.loads(line) for line in raw.splitlines()]
        normalized = {}
        for event in events:
            if event.get("Test") and event.get("Action") in ("pass", "fail", "skip"):
                if normalized.get(event["Test"]) != "fail":
                    normalized[event["Test"]] = event["Action"]
        (root / "results.jsonl").write_text("".join(json.dumps({"Action": a, "Test": t}, separators=(",", ":")) + "\n" for t, a in sorted(normalized.items())))
        report["go_exit_code"] = state["ExitCode"]
        require(state["ExitCode"] == 0, "Go tests returned nonzero; inspect private results")
        outcomes = results(raw)
        evidence = bindings(root / "bindings.jsonl", token)
        require({e["phase"] for e in evidence} == {"base", "blueprint"}, "Run did not exercise construction and deployment")
        report.update(status="passed", tests=len(outcomes), bindings=len(evidence))
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        try:
            cleanup(root)
            report["cleanup"] = "passed"
        except Exception as exc:
            report.update(status="failed", cleanup="failed", cleanup_error=str(exc))
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        save(root / "report.json", report)
    print(json.dumps(report))
    return 0 if report["status"] == "passed" else 1


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("run")
    start.add_argument("directory")
    start.add_argument("--tester", required=True)
    start.add_argument("--testee", required=True)
    start.add_argument("--run", default=SMOKE, help="Go test selector; use .* explicitly for the full appservice suite")
    start.add_argument("--probe", action="store_true", help="Exercise blueprint construction/deployment without appservice assertions")
    start.add_argument("--reject", action="store_true", help="Inject an unsafe inspection response in the live probe; expect exit 1")
    start.add_argument("--hold-seconds", type=int, default=0)
    clean = sub.add_parser("cleanup")
    clean.add_argument("directory")
    args = parser.parse_args()
    if args.command == "cleanup":
        cleanup(Path(args.directory).expanduser().resolve())
        return 0
    require(not args.reject or args.probe, "--reject requires --probe")
    return run(args)


if __name__ == "__main__":
    def stop(signum, frame):
        raise RuntimeError("Interrupted; cleanup requested")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    raise SystemExit(main())
