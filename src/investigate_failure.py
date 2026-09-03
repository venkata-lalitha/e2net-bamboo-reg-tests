"""Investigates one failing Bamboo test by correlating it against the real
E2NA/E2net server logs on dev10091/dev10092.

Usage:
    python src\\investigate_failure.py --list
    python src\\investigate_failure.py --index 3
    python src\\investigate_failure.py --index 3 --build-number 1086
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from config import load_config
from bamboo_client import BambooClient
from ssh_runner import SshRunner

# Known log files across both hosts. Edit here if new logs/hosts are added.
LOG_TARGETS = [
    ("dev10092.dev.e2open.com", "/e2open/var/log/e2na/e2na.log"),
    ("dev10092.dev.e2open.com", "/e2open/var/log/e2netio/e2netio.log"),
    ("dev10092.dev.e2open.com", "/e2open/var/log/scenario/scenario.log"),
    ("dev10092.dev.e2open.com", "/e2open/var/log/e2na/test.log"),
    ("dev10091.dev.e2open.com", "/e2open/var/log/e2net/e2net.log"),
]

BUSINESS_ID_RE = re.compile(r"businessId=([\w]+_[\w]+_\w+_[\w.]+_\w+)")
DUNS_RE = re.compile(r"(\d+)_(\d+)_")


def _test_message(t: dict) -> str:
    errors = t.get("errors", {}).get("error", [])
    if isinstance(errors, dict):
        errors = [errors]
    return errors[0].get("message", "") if errors else ""


def list_failures(tests: list) -> None:
    print(f"TOTAL FAILING: {len(tests)}\n")
    for i, t in enumerate(tests, 1):
        print(f"{i}. {t.get('className', '')} :: {t.get('methodName', '')}")
        print(f"   {_test_message(t)[:150]}")


def investigate(ssh: SshRunner, tests: list, index: int) -> None:
    if index < 1 or index > len(tests):
        print(f"Only {len(tests)} failing tests; index {index} out of range.")
        return

    t = tests[index - 1]
    msg = _test_message(t)
    print(f"--- Investigating #{index}: {t.get('className', '')} :: {t.get('methodName', '')} ---")
    print(msg[:400], "\n")

    bid_match = BUSINESS_ID_RE.search(msg)
    duns_match = DUNS_RE.match(bid_match.group(1)) if bid_match else None

    if not bid_match and not duns_match:
        print("Could not extract a businessId/DUNS pair from this failure's message; "
              "falling back to a generic ERROR-level scan of all known logs instead.")
        for host, path in LOG_TARGETS:
            print(f"\n=== {host}: ERROR-level entries in {path} ===")
            exit_code, out, err = ssh._exec_as_eoadmin(
                host, f"grep -i ' ERROR ' {path} 2>/dev/null | tail -50", timeout=60
            )
            print(out or "(no ERROR-level entries)")
        return

    if bid_match:
        business_id = bid_match.group(1)
        print(f"=== e2na/test.log: DataLoader entries for businessId={business_id} ===")
        exit_code, out, err = ssh._exec_as_eoadmin(
            "dev10092.dev.e2open.com",
            f"grep -n '{business_id}' /e2open/var/log/e2na/test.log 2>/dev/null | head -20",
            timeout=60,
        )
        print(out or "(no matches)")

    if duns_match:
        duns_a, duns_b = duns_match.group(1), duns_match.group(2)
        print(f"\n=== e2net.log: NoSuchB2BProfileException for DUNS pair {duns_a}/{duns_b} ===")
        exit_code, out, err = ssh._exec_as_eoadmin(
            "dev10091.dev.e2open.com",
            f"grep -n -E 'toDuns={duns_b}, fromDuns={duns_a}|toDuns={duns_a}, fromDuns={duns_b}' "
            f"/e2open/var/log/e2net/e2net.log 2>/dev/null | head -20",
            timeout=60,
        )
        print(out or "(no matching NoSuchB2BProfileException for this DUNS pair)")

        print(f"\n=== e2net.log: any lines mentioning either DUNS ({duns_a} or {duns_b}) ===")
        exit_code, out, err = ssh._exec_as_eoadmin(
            "dev10091.dev.e2open.com",
            f"grep -n -E '{duns_a}|{duns_b}' /e2open/var/log/e2net/e2net.log 2>/dev/null | head -30",
            timeout=60,
        )
        print(out or "(no matches)")


def main():
    parser = argparse.ArgumentParser(description="Investigate one failing Bamboo test against the real server logs")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--plan", default="E2NETS-E2NETV3083")
    parser.add_argument("--build-number", default="1086")
    parser.add_argument("--index", type=int, help="Which failing test (1-based) to investigate")
    parser.add_argument("--list", action="store_true", help="List all failing tests with their indices, then exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    bamboo = BambooClient(cfg.bamboo.url, auth_method=cfg.bamboo.auth_method, token=cfg.bamboo.token,
                           username=cfg.bamboo.username, password=cfg.bamboo.password)
    tests = bamboo.failing_tests(args.plan, args.build_number)

    if args.list or not args.index:
        list_failures(tests)
        return

    ssh = SshRunner(
        hosts=cfg.ssh.hosts,
        username=cfg.ssh.username,
        auth_method=cfg.ssh.auth_method,
        key_path=cfg.ssh.key_path,
        password=cfg.ssh.password,
        jump_host=cfg.ssh.jump_host,
    )
    investigate(ssh, tests, args.index)


if __name__ == "__main__":
    main()
