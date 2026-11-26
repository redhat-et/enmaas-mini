#!/usr/bin/env python3
"""Run RHEL acceptance over SSH with mock providers or private real Qwen."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import ssl
import subprocess
import tarfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]


def public_https(hostname, ca, caller, real=False):
    """Exercise real public ingress from the workstation allowed by AWS."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))))
    for token, expected in (("invalid", 401), (caller.read_text().strip(), 200)):
        request = urllib.request.Request(f"https://{hostname}:8443" + ("/vllm/v1/models" if real else "/v1/responses"),
            data=None if real else json.dumps({"model": "fixture", "input": "hello"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            response = opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if response.status != expected:
                raise ValueError(f"public HTTPS: expected {expected}, received {response.status}")
            if expected == 200:
                body = response.read().decode()
                valid = (any(model.get("id") in ("qwen3-8b", "qwen3.8-27b-int4")
                             for model in json.loads(body).get("data", [])) if real else "mock answer" in body)
                if not valid:
                    raise ValueError("public HTTPS did not reach the expected provider")
        time.sleep(1)
    print("PASS: workstation → public verified TLS → JWT denial/acceptance → installed Praxis → " + ("real Qwen model list" if real else "mock"), flush=True)


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def logged(command, output):
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
        for line in process.stdout:
            print(line, end="", flush=True)
            output.write(line)
            output.flush()
        return process.wait()


def reboot(ssh):
    old = run(*ssh, "cat /proc/sys/kernel/random/boot_id", capture_output=True, text=True).stdout.strip()
    result = subprocess.run([*ssh, "sudo -n systemctl reboot"], capture_output=True)
    if result.returncode not in (0, 255):
        raise RuntimeError("reboot command failed")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        time.sleep(5)
        result = subprocess.run([*ssh, "cat /proc/sys/kernel/random/boot_id"], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip() != old:
            print("PASS: reconnected after a verified host reboot", flush=True)
            return
    raise TimeoutError("host did not return with a new boot ID within five minutes")


def bundle():
    output = io.BytesIO()
    manifest = {}
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for directory in ("configs", "scripts", "tests"):
            for path in sorted((ROOT / directory).rglob("*")):
                if {"__pycache__", "node_modules", ".venv"}.intersection(path.relative_to(ROOT).parts) or path.suffix in (".pyc", ".secret", ".local"):
                    continue
                if path.is_symlink():
                    raise ValueError(f"symlink in test bundle: {path}")
                if path.is_file():
                    data = path.read_bytes()
                    name = str(path.relative_to(ROOT))
                    manifest[name] = hashlib.sha256(data).hexdigest()
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(data), path.stat().st_mode & 0o777
                    archive.addfile(info, io.BytesIO(data))
    return output.getvalue(), manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="administrator user@IPv4-or-DNS")
    parser.add_argument("--ssh-key", required=True, type=Path)
    parser.add_argument("--scenario", required=True, choices=("all-in-one", "remote-gateway"))
    parser.add_argument("--profile", choices=("memory", "valkey"), default="memory")
    parser.add_argument("--phase", choices=("install", "mock", "test", "check", "lifecycle", "real-lifecycle", "switch-profile", "providers", "gpu-drivers", "real-setup", "real-test", "mock-again", "features", "features-restore", "all"), default="all")
    parser.add_argument("--feature-provider", choices=("vllm", "cloud"), default="vllm", help="features: which synthetic provider routes to qualify")
    parser.add_argument("--inference", choices=("cpu", "gpu"))
    parser.add_argument("--vllm-image", help="real-setup: candidate NAME@sha256:DIGEST for the selected CPU/GPU mode")
    parser.add_argument("--model", choices=("qwen3-8b", "qwen3.8-27b-int4"),
                        help="real-setup: vLLM preset (default: qwen3-8b); tests use the installed model")
    parser.add_argument("--harness", choices=("codex", "opencode", "claude-code", "claude"), help="real-test/real-lifecycle: API checks plus one native harness; claude is an alias")
    args = parser.parse_args()
    if args.harness == "claude-code":
        args.harness = "claude"
    if args.model is not None and args.phase != "real-setup":
        parser.error("--model applies only to real-setup; real-test reads the installed model")
    if args.phase == "real-setup" and not args.inference:
        parser.error("real-setup needs --inference cpu|gpu")
    if args.vllm_image is not None:
        if args.phase != "real-setup":
            parser.error("--vllm-image applies only to real-setup")
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", args.vllm_image):
            parser.error("--vllm-image requires an immutable NAME@sha256:DIGEST")
    if args.harness and args.phase not in ("real-test", "real-lifecycle", "features"):
        parser.error("--harness applies only to real-test, real-lifecycle or features")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*", args.host):
        parser.error("use an explicit user@host")
    if not args.ssh_key.is_file():
        parser.error("SSH private key does not exist")
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ForwardAgent=no",
           "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10", "-i", str(args.ssh_key), args.host]
    state = ROOT / ".state" / ("rhel-" + args.host.replace("@", "-"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = "secure-single-server-deploy"
    data, manifest = bundle()
    (state / "bundle-sha256.json").write_text(json.dumps(manifest, indent=2) + "\n")
    run(*ssh, f'install -d -m 0700 ~/{destination} && tar -xzf - -C ~/{destination}', input=data)
    if args.scenario == "remote-gateway":
        tool = ROOT / "scripts/remote-gateway/credentials"
        tls = state / "tls-local-client"
        if not tls.exists():
            run(tool, "lab-tls", "--directory", tls, "--hostname", args.host.split("@", 1)[1],
                "--alt-hostname", "127.0.0.1")
        if not (state / "issuer").exists():
            run(tool, "init-jwt", "--directory", state / "issuer")
        caller = state / ("caller-" + str(time.time_ns()) + ".jwt")
        run(tool, "issue", "--key", state / "issuer/private.pem", "--subject", "rhel-smoke",
            "--days", "1", "--output", caller)
        material = io.BytesIO()
        with tarfile.open(fileobj=material, mode="w:gz") as archive:
            for filename, path in {"tls.pem": tls / "server.pem", "tls-key.pem": tls / "server-key.pem",
                                   "jwt-public.pem": state / "issuer/public.pem", "ca.pem": tls / "ca.pem",
                                   "caller.jwt": caller}.items():
                payload = path.read_bytes()
                info = tarfile.TarInfo(filename)
                info.size, info.mode = len(payload), 0o600
                archive.addfile(info, io.BytesIO(payload))
        run(*ssh, f'install -d -m 0700 ~/{destination}/material && tar -xzf - -C ~/{destination}/material',
            input=material.getvalue())
    command = ["sudo", "-n", "python3", "tests/rhel/host.py", "--scenario", args.scenario,
               "--profile", args.profile, "--phase", "check" if args.phase in ("lifecycle", "real-lifecycle") else args.phase,
               "--hostname", args.host.split("@", 1)[1]]
    if args.inference:
        command += ["--inference", args.inference]
    if args.vllm_image:
        command += ["--vllm-image", args.vllm_image]
    if args.model:
        command += ["--model", args.model]
    if args.harness:
        command += ["--harness", args.harness]
    if args.phase == "features":
        command += ["--feature-provider", args.feature_provider]
    started = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log = state / f"{started}-{args.scenario}-{args.profile}-{args.phase}.log"
    log.with_suffix(".bundle.json").write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        with log.open("w") as output:
            code = logged([*ssh, f"cd ~/{destination} && " + shlex.join(command)], output)
            if code == 0 and args.phase in ("lifecycle", "real-lifecycle"):
                reboot(ssh)
                output.write("PASS: reconnected after verified host reboot\n")
                for phase in ("check", "real-test" if args.phase == "real-lifecycle" else "test"):
                    command[command.index("--phase") + 1] = phase
                    code = logged([*ssh, f"cd ~/{destination} && " + shlex.join(command)], output)
                    if code:
                        raise SystemExit(code)
                print("PASS: host and real harness checks after reboot", flush=True)
            if code == 0 and args.scenario == "remote-gateway" and args.phase in ("real-setup", "real-test", "real-lifecycle"):
                public_https(args.host.split("@", 1)[1], tls / "ca.pem", caller, real=True)
                output.write("PASS: public TLS/JWT with real Qwen model list\n")
            if code == 0 and args.scenario == "remote-gateway" and args.phase in ("test", "all", "lifecycle", "switch-profile", "providers", "mock-again"):
                public_https(args.host.split("@", 1)[1], tls / "ca.pem", caller)
                output.write("PASS: workstation public HTTPS, verified TLS, JWT denial and synthetic inference\n")
    finally:
        print(f"Evidence: {log}")
    raise SystemExit(code)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"error: {error}") from None
