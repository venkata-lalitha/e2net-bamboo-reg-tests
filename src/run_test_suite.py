"""Manually triggers runenginetest-withport.sh for a specific test suite on
dev10092 (the only host this suite runs on), hopping through ssh.jump_host
(as eoadmin) since dev10092 doesn't accept direct password auth. Useful for
re-running just the suite that's failing, without needing Bamboo to kick
off a whole new build.

After running the plan and checking its logs, restarts the e2net-test
docker app (`e2net-docker.sh stop -a e2net-test && start -a e2net-test`)
since the regtests folder mount is known to go missing most of the time
otherwise. Pass --skip-restart to skip that step.

Usage:
    python src\\run_test_suite.py --testsuite /e2open/app/e2net-gateway-integtest/tests/regtests/core/core.plan
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from config import load_config
from ssh_runner import SshRunner

TARGET_HOST = "dev10092.dev.e2open.com"


def restart_e2net_test(ssh: SshRunner) -> None:
    """Stops then starts the e2net-test docker app, since the regtests
    folder mount is known to go missing most of the time otherwise.
    """
    print("Stopping e2net-test docker app...")
    exit_code, out, err = ssh._exec_as_eoadmin(TARGET_HOST, "e2net-docker.sh stop -a e2net-test", timeout=300)
    print(f"  exit_code={exit_code}")
    if out.strip():
        print(out[-2000:])

    print("Starting e2net-test docker app...")
    # The start script prompts for a version to start; pipe blank lines so
    # it accepts the default at every prompt instead of hanging forever.
    exit_code, out, err = ssh._exec_as_eoadmin(
        TARGET_HOST, "yes '' | e2net-docker.sh start -a e2net-test", timeout=300
    )
    print(f"  exit_code={exit_code}")
    if out.strip():
        print(out[-2000:])

    print("Checking e2net-test container status...")
    exit_code, out, err = ssh._exec_as_eoadmin(TARGET_HOST, "docker ps --filter name=e2net-test --format '{{.Names}}: {{.Status}}'")
    print(out or "(no e2net-test container found running)")


def main():
    parser = argparse.ArgumentParser(description="Manually run the E2NA regression suite for one test plan")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--testsuite", required=True, help="Full path to the .plan/.suite file to run")
    parser.add_argument("--timeout", type=int, default=600, help="Suite timeout in seconds, passed to the script itself")
    parser.add_argument("--skip-restart", action="store_true", help="Skip the e2net-test docker restart pre-step")
    args = parser.parse_args()

    cfg = load_config(args.config)
    ssh = SshRunner(
        hosts=cfg.ssh.hosts,
        username=cfg.ssh.username,
        auth_method=cfg.ssh.auth_method,
        key_path=cfg.ssh.key_path,
        password=cfg.ssh.password,
        jump_host=cfg.ssh.jump_host,
    )

    print(f"=== ls -la {args.testsuite} on {TARGET_HOST} ===")
    exit_code, out, err = ssh._exec_as_eoadmin(TARGET_HOST, f"ls -la {args.testsuite} 2>&1")
    print(out)


    # buildhost uses the short hostname to match the script's internal
    # convention (matches how it appears in the wget/test.log output).
    short_host = TARGET_HOST.split(".")[0]
    command = (
        "/e2open/home/cmbuild/regtests/E2NET/runenginetest-withport.sh "
        "testdir=/e2open/app/e2net-gateway-integtest/tests "
        "testlog=/e2open/var/log/e2na/test.log "
        f"buildhost={short_host} "
        f"timeout={args.timeout} "
        f"testsuite={args.testsuite}"
    )
    print(f"Running on {TARGET_HOST} (via jump_host {cfg.ssh.jump_host}):\n  {command}\n")

    exit_code, out, err = ssh._exec_as_eoadmin(TARGET_HOST, command, timeout=args.timeout + 60)
    print(f"exit_code={exit_code}")
    if out.strip():
        print("--- stdout (tail) ---")
        print(out[-4000:])
    if err.strip():
        print("--- stderr (tail) ---")
        print(err[-2000:])

    print("\n--- cireport.xml content ---")
    exit_code, out, err = ssh._exec_as_eoadmin(
        TARGET_HOST,
        "cat /e2open/app/e2net-gateway-integtest/tests/cireport.xml 2>/dev/null",
    )
    print(out or "(cireport.xml not found or empty)")

    print("\n--- e2na.log: tail right after this run (setup/load error detail) ---")
    exit_code, out, err = ssh._exec_as_eoadmin(
        TARGET_HOST,
        "tail -80 /e2open/var/log/e2na/e2na.log 2>/dev/null",
    )
    print(out or "(no e2na.log found)")

    if not args.skip_restart:
        print("\n--- restarting e2net-test (post-run, to fix the regtests mount for next time) ---")
        restart_e2net_test(ssh)


if __name__ == "__main__":
    main()
