"""Exercise the packaged solver as its non-root user, without a network or secrets."""

import argparse
import json
import subprocess
from pathlib import Path
from uuid import uuid4

NODE_VERSION = "v22.23.2"
RUNTIME = "/app/nesting-runtime/"


def container(image, args, payload=None):
    name = "werco-nesting-smoke-" + uuid4().hex
    command = [
        "docker",
        "run",
        "--rm",
        "--name=" + name,
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--memory=1g",
        "--pids-limit=64",
        "-i",
        "--entrypoint=/usr/local/bin/node",
        image,
        "--max-old-space-size=512",
        *args,
    ]
    try:
        return subprocess.run(command, input=payload, text=True, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        # Killing the docker CLI alone can leave its container computing. Remove
        # only this invocation's randomly named container, never unrelated work.
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=10, check=False)
        raise


def fixture():
    return {
        "version": 4,
        "units": "in",
        "currency": "USD",
        "name": "Image smoke",
        "activeGroupId": "group",
        "groups": [
            {
                "id": "group",
                "quote": {
                    "version": 3,
                    "units": "in",
                    "currency": "USD",
                    "name": "Image smoke",
                    "material": "Carbon steel",
                    "thickness": 0.125,
                    "margin": 0.125,
                    "gap": 0.125,
                    "objective": "area",
                    "spacingMode": "manual",
                    "parts": [
                        {
                            "id": "plate",
                            "name": "Synthetic plate",
                            "quantity": 2,
                            "rotate": True,
                            "color": 0,
                            "loops": [
                                {
                                    "type": "poly",
                                    "points": [
                                        {"x": 0, "y": 0},
                                        {"x": 1, "y": 0},
                                        {"x": 1, "y": 2},
                                        {"x": 0, "y": 2},
                                    ],
                                }
                            ],
                        }
                    ],
                    "options": [{"id": "sheet", "enabled": True, "price": None, "width": 6, "height": 6}],
                },
            }
        ],
    }


def smoke(image):
    inspect = container(
        image,
        [
            "-e",
            (
                'const fs=require("node:fs"),c=require("node:crypto"),p="' + RUNTIME + '";'
                'const manifest=JSON.parse(fs.readFileSync(p+"manifest.json"));'
                'console.log(JSON.stringify({manifest,node_version:process.version,uid:process.getuid(),'
                'actual_sha256:c.createHash("sha256").update(fs.readFileSync(p+"solver.cjs")).digest("hex")}));'
            ),
        ],
    )
    assert inspect.returncode == 0 and not inspect.stderr, "Packaged Node runtime failed"
    identity = json.loads(inspect.stdout)
    manifest = identity["manifest"]
    assert identity["uid"] != 0, "The solver must run as the worker's non-root user"
    assert identity["node_version"] == NODE_VERSION
    assert manifest["protocol"] == 1 and manifest["node_major"] == 22
    assert manifest["solver_version"] == "werco-contour-v4"
    assert manifest["entrypoint"] == "solver.cjs" and manifest["max_option_evaluations"] == 36
    assert manifest["bundle_sha256"] == identity["actual_sha256"]
    payload = {"protocol": 1, "input_sha256": "a" * 64, "estimate": fixture()}
    run = container(image, [RUNTIME + "solver.cjs"], json.dumps(payload))
    assert run.returncode == 0 and not run.stderr, "Packaged solver failed the synthetic geometry case"
    messages = [json.loads(line) for line in run.stdout.splitlines()]
    assert len(messages) == 3
    hello, option, summary = messages
    assert hello == {
        "type": "hello",
        "protocol": 1,
        "input_sha256": "a" * 64,
        "units": "mm",
        "solver_version": manifest["solver_version"],
        "bundle_sha256": manifest["bundle_sha256"],
        "node_version": NODE_VERSION,
    }
    assert option["type"] == "option" and option["requested"] == 2
    assert option["result"]["complete"] and len(option["result"]["nest"]["placements"]) == 2
    assert summary["type"] == "summary" and summary["stop_reason"] == "completed"
    assert summary["evaluated_count"] == summary["complete_option_count"] == summary["total_options"] == 1
    payload["estimate"]["units"] = "mm"
    invalid = container(image, [RUNTIME + "solver.cjs"], json.dumps(payload))
    assert invalid.returncode == 2 and not invalid.stderr
    rejected = [json.loads(line) for line in invalid.stdout.splitlines()]
    assert [message["type"] for message in rejected] == ["hello", "error"]
    assert rejected[-1]["code"] == "invalid_geometry"
    return {**manifest, "node_version": NODE_VERSION}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--manifest-out", type=Path, required=True)
    args = parser.parse_args()
    identity = smoke(args.image)
    args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
    args.manifest_out.write_text(json.dumps(identity, indent=2) + "\n")
    print("Non-root, network-disabled worker image smoke passed; bundle " + identity["bundle_sha256"])


if __name__ == "__main__":
    main()
