#!/usr/bin/env python3
"""Measure loopback callback forwarding with prestarted clients, without homeservers."""
import argparse
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid

IMAGE = "sha256:7415fbc3c9e4979cc717d92377ab2bc7b2b4a2af1ac03cc52b5f3f88efedaf3a"
LABEL = "sh.terva.callback-probe"
ROUNDS = 5


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def save(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=2) + "\n")
    tmp.replace(path)


def wait(path, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out waiting for " + path.name)
        time.sleep(0.005)
    return json.loads(path.read_text())


def worker(role, root):
    address = "127.0.0.1" if role == "local" else "host.docker.internal"
    ip = socket.gethostbyname(address) if role != "server" else None
    save(root / (role + "-ready.json"), {"ip": ip})
    wait(root / "start.json")
    for number in range(ROUNDS):
        prefix = str(number)
        if role == "server":
            body = ("callback-control-" + prefix).encode()

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            require(server.server_address[0] == "127.0.0.1", "Listener is not loopback")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                save(root / (prefix + "-listener.json"), {
                    "binding": list(server.server_address), "ready": time.monotonic(),
                })
                for client in ("local", "alias", "bridge"):
                    wait(root / (prefix + "-" + client + ".json"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        else:
            listener = wait(root / (prefix + "-listener.json"))
            attempts = []
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                start = time.monotonic()
                connection = http.client.HTTPConnection(ip, listener["binding"][1], timeout=0.5)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    require(response.status == 200, "Unexpected control status")
                    require(response.read() == ("callback-control-" + prefix).encode(), "Wrong control listener")
                    result = "success"
                except (OSError, http.client.HTTPException) as exc:
                    result = type(exc).__name__
                finally:
                    connection.close()
                attempts.append({"since_ready_ms": round((start - listener["ready"]) * 1000, 3),
                                 "result": result})
                if result == "success":
                    break
                time.sleep(0.02)
            save(root / (prefix + "-" + role + ".json"), {"ip": ip, "attempts": attempts})


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError("Docker operation failed: " + args[0])
    return result.stdout.strip()


def control(root):
    checkout = Path(__file__).resolve().parents[2]
    if root.is_relative_to(checkout) or root.exists() or not root.parent.is_dir():
        raise RuntimeError("Choose a new private directory outside Git below an existing parent")
    if any(c in str(root) for c in (",", "\n")):
        raise RuntimeError("Unsupported mount path")
    image = json.loads(docker("image", "inspect", IMAGE))[0]["Id"]
    require(image == IMAGE, "Unexpected control image")
    root.mkdir(mode=0o700)
    token = uuid.uuid4().hex[:12]
    label = LABEL + "=" + token
    containers, network = [], None
    report = {"run_id": token, "image": image, "status": "failed",
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        network = docker("network", "create", "--label", label, "terva-callback-" + token)
        save(root / "owner.json", {"run_id": token, "network": network, "containers": containers})
        for role in ("server", "local", "alias", "bridge"):
            cid = docker("create", "--name", "terva-callback-" + token + "-" + role,
                         "--label", label, "--network", network if role == "bridge" else "host",
                         "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                         "--memory=64m", "--pids-limit=32", "--cpus=1",
                         "--mount", "type=bind,src=" + str(Path(__file__).resolve()) + ",dst=/probe.py,readonly",
                         "--mount", "type=bind,src=" + str(root) + ",dst=/output",
                         image, "python", "-B", "/probe.py", "/output", "--worker", role)
            containers.append(cid)
            save(root / "owner.json", {"run_id": token, "network": network, "containers": containers})
            docker("start", cid)
        for role in ("server", "local", "alias", "bridge"):
            wait(root / (role + "-ready.json"))
        save(root / "start.json", {})
        measurements = []
        for number in range(ROUNDS):
            row = {"round": number, "listener": wait(root / (str(number) + "-listener.json"))}
            for role in ("local", "alias", "bridge"):
                result = wait(root / (str(number) + "-" + role + ".json"))
                require(result["attempts"][-1]["result"] == "success", "Callback never became reachable")
                row[role] = result
            measurements.append(row)
        for cid in containers:
            require(docker("wait", cid) == "0", "Probe worker failed")
        report.update(status="passed", measurements=measurements)
    finally:
        for cid in reversed(containers):
            item = json.loads(docker("inspect", cid))[0]
            require(item["Config"]["Labels"].get(LABEL) == token, "Container ownership mismatch")
            (root / (cid[:12] + ".log")).write_text(docker("logs", cid))
            docker("rm", "-f", item["Id"])
        if network:
            item = json.loads(docker("network", "inspect", network))[0]
            require(item["Labels"].get(LABEL) == token, "Network ownership mismatch")
            docker("network", "rm", item["Id"])
        require(not docker("ps", "-aq", "--filter", "label=" + label), "Owned container remains")
        require(not docker("network", "ls", "-q", "--filter", "label=" + label), "Owned network remains")
        report["cleanup"] = "passed"
        save(root / "report.json", report)
    print(json.dumps({"status": report["status"], "cleanup": report["cleanup"], "rounds": [
        {"port": r["listener"]["binding"][1], **{role: {
            "attempts": len(r[role]["attempts"]), "first_success_ms": r[role]["attempts"][-1]["since_ready_ms"]
        } for role in ("local", "alias", "bridge")}} for r in measurements]}))


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--worker", choices=("server", "local", "alias", "bridge"))
    args = parser.parse_args()
    root = Path(args.directory).expanduser().resolve()
    if args.worker:
        worker(args.worker, root)
    else:
        control(root)
