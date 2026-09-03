"""Bamboo Regression Agent - orchestrator.

Workflow per plan:
  1. Check Bamboo for latest build status; skip if not failed and
     only_if_failed is true.
  2. Fetch the failing-tests summary directly from Bamboo's own REST API
     (the remote test framework's log format isn't machine-parseable, so
     Bamboo's own test-result aggregation is the source of truth instead
     of SSHing in and parsing raw logs/XML).
  3. Locate the matching source file in the local repo(s) and run the rule
     engine to diagnose root cause for each failing test.
  4. For high-confidence diagnoses with a patch, apply the fix locally
     (git working tree only - no commit/push) if fix.auto_apply is set.
  5. Deploy any applied fix into the containers via docker cp, then
     trigger one fresh SSH run of the plan so the fix is exercised (its
     output isn't parsed - check Bamboo's next build to confirm the fix).
  6. Write a consolidated Markdown report.

Usage:
    python src/main.py --config config.yaml
    python src/main.py --config config.yaml --plan TODO-PLAN-KEY-1 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from config import load_config, AppConfig
from bamboo_client import BambooClient
from ssh_runner import SshRunner
from junit_parser import TestFailure
import fix_rules
import fixer
from source_locator import find_source_file_multi


def _repo_for_file(cfg: AppConfig, local_file_path: str):
    """Finds which configured repo (e2net-platform, e2net-commons, ...) a
    local file path belongs to, by longest matching local_path prefix.
    """
    norm_file = os.path.normcase(os.path.normpath(local_file_path))
    best = None
    for repo in cfg.repo.paths:
        norm_root = os.path.normcase(os.path.normpath(repo.local_path))
        if norm_file == norm_root or norm_file.startswith(norm_root + os.sep):
            if best is None or len(norm_root) > len(os.path.normcase(os.path.normpath(best.local_path))):
                best = repo
    return best


def deploy_patch(cfg: AppConfig, ssh: SshRunner, local_file_path: str) -> list:
    """Ships a locally-patched file to the containers on every SSH host via
    `docker cp`. Returns the list of (host, container_name) pairs the file
    was successfully deployed to.
    """
    repo = _repo_for_file(cfg, local_file_path)
    if repo is None:
        print(f"Warning: {local_file_path} doesn't match any configured repo.paths; skipping deploy")
        return []

    rel_path = os.path.relpath(local_file_path, repo.local_path).replace(os.sep, "/")
    container_dest_path = f"{repo.container_base_path.rstrip('/')}/{rel_path}"
    deployed_to = []
    for host in cfg.ssh.hosts:
        for container_name in cfg.deploy.host_containers.get(host, []):
            try:
                ssh.deploy_file(host, local_file_path, cfg.deploy.remote_tmp_dir, container_name, container_dest_path)
                print(f"Deployed {rel_path} -> {host}:{container_name}:{container_dest_path}")
                deployed_to.append((host, container_name))
            except RuntimeError as e:
                print(f"Warning: failed to deploy {rel_path} to {host}:{container_name}: {e}")
    return deployed_to


def build_clients(cfg: AppConfig):
    """Builds the (BambooClient|None, SshRunner) pair shared by the CLI
    entrypoint and the webhook listener.
    """
    bamboo_authenticated = (
        (cfg.bamboo.auth_method == "basic" and cfg.bamboo.username and cfg.bamboo.password)
        or (cfg.bamboo.auth_method == "token" and cfg.bamboo.token)
    )
    bamboo = (
        BambooClient(
            cfg.bamboo.url,
            auth_method=cfg.bamboo.auth_method,
            token=cfg.bamboo.token,
            username=cfg.bamboo.username,
            password=cfg.bamboo.password,
        )
        if bamboo_authenticated
        else None
    )
    ssh = SshRunner(
        hosts=cfg.ssh.hosts,
        username=cfg.ssh.username,
        auth_method=cfg.ssh.auth_method,
        key_path=cfg.ssh.key_path,
        password=cfg.ssh.password,
        jump_host=cfg.ssh.jump_host,
    )
    return bamboo, ssh


def _bamboo_test_to_failure(t: dict) -> TestFailure:
    """Converts one entry from BambooClient.failing_tests() into the
    TestFailure shape fix_rules.diagnose() expects.
    """
    classname = t.get("className") or t.get("class_name") or ""
    test_name = t.get("methodName") or t.get("method_name") or t.get("name") or ""
    errors = t.get("errors", {}).get("error", [])
    if isinstance(errors, dict):
        errors = [errors]
    messages = [e.get("message", "") for e in errors if isinstance(e, dict) and e.get("message")]
    return TestFailure(
        classname=classname,
        test_name=test_name,
        kind="failure",
        message=messages[0] if messages else "",
        stacktrace="\n".join(messages),
        source_file="bamboo-api",
    )


def process_plan(cfg: AppConfig, bamboo: BambooClient | None, ssh: SshRunner, plan_key: str, dry_run: bool, build_number: str = "latest"):
    report_entries = []

    if not bamboo:
        print(f"[{plan_key}] Bamboo client not configured; cannot fetch test results.")
        return report_entries

    if cfg.bamboo.only_if_failed:
        try:
            if not bamboo.is_failed(plan_key, build_number):
                print(f"[{plan_key}] Bamboo build {build_number} is not failed, skipping.")
                return report_entries
        except RuntimeError as e:
            print(f"[{plan_key}] Warning: could not check Bamboo status ({e}); proceeding anyway.")

    print(f"[{plan_key}] Fetching failing tests from Bamboo for build {build_number}...")
    try:
        bamboo_failures = bamboo.failing_tests(plan_key, build_number)
    except RuntimeError as e:
        print(f"[{plan_key}] Error fetching Bamboo test results: {e}")
        return report_entries

    if not bamboo_failures:
        print(f"[{plan_key}] Bamboo reports no failing tests for build {build_number}. Suite passing.")
        report_entries.append(
            {"plan": plan_key, "status": "PASSING", "attempt": 1, "diagnoses": []}
        )
        return report_entries

    failures = [_bamboo_test_to_failure(t) for t in bamboo_failures]
    diagnoses = []
    any_patch_applied = False
    deployed_targets = set()
    for failure in failures:
        source_file = find_source_file_multi([r.local_path for r in cfg.repo.paths], failure.classname)
        diag = fix_rules.diagnose(failure, source_file)
        entry = {
            "classname": failure.classname,
            "test_name": failure.test_name,
            "source_file": source_file,
            "diagnosis": diag,
        }
        diagnoses.append(entry)

        if diag.patch and diag.confidence == "high" and cfg.fix.auto_apply and not dry_run:
            applied = fixer.apply_patch(diag.patch, create_backup=cfg.fix.create_backup)
            entry["patch_applied"] = applied
            if applied:
                any_patch_applied = True
                print(f"[{plan_key}] Applied fix to {diag.patch.file_path}")
                if cfg.deploy.enabled:
                    deployed_targets.update(deploy_patch(cfg, ssh, diag.patch.file_path))
            else:
                print(f"[{plan_key}] Could not safely apply fix to {diag.patch.file_path} (ambiguous match)")
        else:
            entry["patch_applied"] = False

    report_entries.append(
        {"plan": plan_key, "status": "FAILING", "attempt": 1, "diagnoses": diagnoses}
    )

    if any_patch_applied and not dry_run:
        for host, container_name in deployed_targets:
            try:
                ssh.restart_container(host, container_name)
                print(f"[{plan_key}] Restarted {container_name} on {host} to pick up the deployed fix")
            except RuntimeError as e:
                print(f"[{plan_key}] Warning: failed to restart {container_name} on {host}: {e}")

        print(f"[{plan_key}] Queuing a fresh Bamboo build to re-run the suite and verify the fix...")
        try:
            queued = bamboo.queue_build(plan_key)
            print(f"[{plan_key}] Queued Bamboo build: {queued}")
        except RuntimeError as e:
            print(f"[{plan_key}] Warning: failed to queue Bamboo build: {e}")

    return report_entries


def write_report(all_entries, report_path):
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    lines = [f"# Bamboo Regression Root Cause Report", f"Generated: {datetime.now().isoformat()}", ""]
    for plan_result in all_entries:
        for entry in plan_result:
            lines.append(f"## Plan: {entry['plan']} (attempt {entry['attempt']}) - {entry['status']}")
            if not entry["diagnoses"]:
                lines.append("No failures.\n")
                continue
            for d in entry["diagnoses"]:
                diag = d["diagnosis"]
                lines.append(f"### {d['classname']}#{d['test_name']}")
                lines.append(f"- Source file: `{d['source_file']}`")
                lines.append(f"- Rule: `{diag.rule_name}` (confidence: {diag.confidence})")
                lines.append(f"- Root cause: {diag.root_cause}")
                lines.append(f"- Suggestion: {diag.suggestion}")
                if diag.patch:
                    lines.append(
                        f"- Patch: `{diag.patch.old_text}` -> `{diag.patch.new_text}` "
                        f"(applied: {d.get('patch_applied', False)})"
                    )
                lines.append("")
    report_text = "\n".join(lines)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    print("\n" + report_text + "\n")
    print(f"(also written to {report_path})")


def main():
    parser = argparse.ArgumentParser(description="Bamboo regression root-cause & auto-fix agent")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--plan", action="append", help="Only process this plan key (repeatable)")
    parser.add_argument("--build-number", default="latest", help="Specific Bamboo build number to check (default: latest)")
    parser.add_argument("--dry-run", action="store_true", help="Diagnose only, never modify files")
    args = parser.parse_args()

    cfg = load_config(args.config)

    bamboo, ssh = build_clients(cfg)

    plans = args.plan or cfg.bamboo.plans
    all_entries = []
    for plan_key in plans:
        all_entries.append(process_plan(cfg, bamboo, ssh, plan_key, dry_run=args.dry_run, build_number=args.build_number))

    write_report(all_entries, cfg.report_path)


if __name__ == "__main__":
    main()
