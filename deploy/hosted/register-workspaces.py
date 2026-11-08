#!/usr/bin/env python3
"""Register the two hosted Beads projects with the local beads-ui process."""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

WORKSPACES = (
    Path("/var/lib/beads/projects/projects/home"),
    Path("/var/lib/beads/projects/projects/system"),
)
ENDPOINT = "http://127.0.0.1:3000/api/register-workspace"


def registrations():
    result = []
    for workspace in WORKSPACES:
        project = workspace.resolve(strict=True)
        metadata = project / ".beads" / "metadata.json"
        if not metadata.is_file():
            raise RuntimeError(f"required Beads metadata missing: {metadata}")
        result.append({"path": str(project), "database": str((project / ".beads").resolve())})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="validate the two workspace metadata files without connecting",
    )
    args = parser.parse_args()
    try:
        workspaces = registrations()
        if args.self_check:
            print(f"validated {len(workspaces)} hosted workspace metadata files")
            return 0
        deadline = time.monotonic() + 20
        for workspace in workspaces:
            body = json.dumps(workspace).encode("utf-8")
            request = Request(ENDPOINT, data=body, headers={"Content-Type": "application/json"})
            while True:
                try:
                    with urlopen(request, timeout=2) as response:
                        result = json.loads(response.read())
                    if result.get("ok") is not True:
                        raise RuntimeError(
                            f"UI rejected workspace registration: {workspace['path']}"
                        )
                    break
                except (URLError, TimeoutError, json.JSONDecodeError) as error:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            f"UI registration failed for {workspace['path']} after 20 seconds: "
                            f"{error}"
                        ) from error
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
        print(f"registered {len(workspaces)} hosted workspaces")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"register-workspaces: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
