#!/usr/bin/env python3
"""Test gateway JWT authentication and local harness configuration with Podman."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests/common"), str(ROOT / "scripts/common")]
from gateway import Gateway, ENGINE, IMAGE, MOCK_IMAGE, bind, run, token
from provider_config import render
from contracts import check
from evidence import save

spec = importlib.util.spec_from_file_location("remote_client", Path(__file__).with_name("run.py"))
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class LocalGateway(Gateway):
    def fixture_command(self):
        # Copy the test tree into the work dir so the bind mount can be labeled
        # for SELinux-enforcing hosts without relabeling the checkout.
        tests = self.work / "tests"
        shutil.copytree(ROOT / "tests", tests,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return [*bind(tests, "/tests"),
                MOCK_IMAGE, "python3", "/tests/remote-client/provider.py"]

    def boot(self):
        self.config = render(self.config, vllm=True, openai=True, anthropic=True)
        for chain in self.config["filter_chains"]:
            for item in chain["filters"]:
                if item["filter"] == "load_balancer":
                    for cluster in item["clusters"]:
                        if cluster["name"] == "vllm":
                            cluster["endpoints"] = ["127.0.0.1:8000"]
                if item["filter"] == "token_rate_limit":
                    item["rules"][0]["capacity"] = 100000
        (self.work / "gateway.json").write_text(json.dumps(self.config))
        super().boot()

    def provider_records(self, provider, reset=False):
        if provider != "vllm":
            return self.control("ok", reset=True) if reset else self.records()
        code = ('import json,urllib.request; r=urllib.request.Request("http://127.0.0.1:19001/'
                + ('scenario",data=b\'{"mode":"ok","reset":true}\'' if reset else 'state",data=None')
                + '); print(urllib.request.urlopen(r).read().decode())')
        data = json.loads(run(ENGINE, "exec", self.name + "-mock", "python3", "-c", code))
        return data if reset else data["records"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("all", "vllm", "openai", "anthropic"), default="all")
    parser.add_argument("--harness", choices=("codex", "opencode", "claude"))
    parser.add_argument("--api-only", action="store_true", help="check real gateway TLS/JWT and APIs without starting CLIs")
    parser.add_argument("--valkey", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("run as an ordinary user")
    output = ROOT / ".state" / ("local-remote-client-" + str(time.time_ns()))
    output.mkdir(mode=0o700, parents=True)
    result = {"status": "running", "scope": "local container gateway and client; synthetic model",
              "engine": ENGINE, "image": IMAGE, "profile": "valkey" if args.valkey else "memory",
              "harnesses_requested": not args.api_only, "providers": {}}
    try:
        with LocalGateway("remote", result["profile"]) as gateway:
            url = "https://localhost:" + gateway.openai_port
            valid = token(gateway.key, exp=int(time.time()) + 3600)
            token_file = output / "caller.jwt"
            client.private_write(token_file, valid)
            ca = output / "ca.pem"
            client.private_write(ca, (gateway.work / "tls/ca.pem").read_text())
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(
                (gateway.work / "tls/server.pem").read_text())).hexdigest()
            header, payload, signature = valid.split(".")
            invalid_signature = header + "." + payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
            rejected = {"missing": "", "malformed": "invalid", "invalid-signature": invalid_signature,
                        "expired": token(gateway.key, exp=int(time.time()) - 600),
                        "wrong-audience": token(gateway.key, aud="not-this-gateway"),
                        "wrong-issuer": token(gateway.key, iss="https://wrong-issuer.test")}
            tokens = [valid, *[value for value in rejected.values() if value]]
            providers = ("vllm", "openai", "anthropic") if args.provider == "all" else (args.provider,)
            for provider in providers:
                model = "qwen3-8b" if provider == "vllm" else "fixture"
                remote = client.Remote(url, ca, valid, provider, model, False)
                remote.verify_tls(fingerprint)
                report = result["providers"][provider] = {"auth": {}, "apis": {}, "harnesses": client.harness_matrix(provider)}
                paths = ("/v1/messages",) if provider == "anthropic" else ("/v1/responses", "/v1/chat/completions")
                if provider == "vllm":
                    paths += ("/v1/messages",)
                gateway.provider_records(provider, reset=True)
                for path in paths:
                    for label, caller in rejected.items():
                        assert remote.request(path, {"model": model}, token=caller)[0] == 401, (provider, path, label)
                        report["auth"][path + ":" + label] = "rejected"
                assert not gateway.provider_records(provider), "rejected caller reached the model"
                for path in paths:
                    for stream in (False, True):
                        request = {"model": model, "input": "hello", "messages": [],
                                   "max_tokens": 4, "stream": stream}
                        # These legacy JWT profiles do not inject stream options.
                        # Chat usage is opt-in; the contract below verifies it.
                        if path == "/v1/chat/completions" and stream:
                            request["stream_options"] = {"include_usage": True}
                        status, body = remote.request(path, request)
                        assert status == 200, (provider, path, status)
                        check(path, body, stream, model=model)
                        report["apis"][path + ":stream=" + str(stream)] = "passed"
                print("PASS gateway TLS/JWT: " + provider, flush=True)
                if args.api_only:
                    continue
                selected = (args.harness,) if args.harness else client.HARNESSES
                options = SimpleNamespace(url=url, ca_file=ca, token_file=token_file, provider=provider, model=model, mode="mock")
                directory = output / provider
                directory.mkdir(mode=0o700)
                for name in selected:
                    gateway.provider_records(provider, reset=True)
                    client.run_harnesses((name,), options, {"inference": "mock"}, directory, tokens, report)
                    if report["harnesses"][name]["status"] == "blocked":
                        continue
                    records = gateway.provider_records(provider)
                    assert records and all(item["credential_ok"] for item in records), "caller leaked to provider or wrong upstream credential"
                    assert any(item["stream"] for item in records) and any(item["continuation"] for item in records), "missing streamed tool continuation"
                    client.private_write(directory / (name + "-requests.json"), json.dumps(records, indent=2))
            complete = all(row["status"] == "passed" for provider in result["providers"].values()
                           for row in provider["harnesses"].values())
            result["status"] = "passed-api-checks" if args.api_only else ("passed" if complete else "partial")
    except BaseException as error:
        result["status"] = "failed"
        result["error"] = client.redact(str(error), locals().get("tokens", []))
        raise
    finally:
        client.private_write(output / "result.json", json.dumps(result, indent=2) + "\n")
        save(f"remote-client-local-{ENGINE}-{'api' if args.api_only else 'harnesses'}", result)
        print("Local gateway evidence: " + str(output), flush=True)


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Run without Python -O")
    def interrupted(*_):
        raise InterruptedError("local gateway test interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    main()
