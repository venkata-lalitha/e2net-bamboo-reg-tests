"""Load and validate config.yaml, resolving secrets from environment variables."""
from __future__ import annotations

import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


def _load_env_file(path: str = ".env") -> None:
    """Loads KEY=VALUE lines from a local, gitignored .env file into
    os.environ (without overriding anything already set in the real
    environment), so credentials don't need to be re-typed every session.
    Never commit a filled-in .env - see .env.example for the expected keys.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass
class BambooConfig:
    url: str
    auth_method: str  # "token" | "basic"
    token: Optional[str]
    username: Optional[str]
    password: Optional[str]
    plans: list
    only_if_failed: bool


@dataclass
class SshConfig:
    hosts: list
    username: str
    auth_method: str
    key_path: str
    password: Optional[str]
    jump_host: Optional[str]


@dataclass
class RepoPath:
    name: str
    local_path: str
    container_base_path: str


@dataclass
class RepoConfig:
    paths: list  # list[RepoPath]


@dataclass
class TestExecConfig:
    remote_command: str
    remote_results_dir: str
    local_results_dir: str


@dataclass
class FixConfig:
    auto_apply: bool
    create_backup: bool
    max_iterations_per_plan: int


@dataclass
class DeployConfig:
    enabled: bool
    host_containers: dict  # host -> list of container names running there
    remote_tmp_dir: str


@dataclass
class WebhookConfig:
    enabled: bool
    host: str
    port: int
    token: Optional[str]


@dataclass
class AppConfig:
    bamboo: BambooConfig
    ssh: SshConfig
    repo: RepoConfig
    test_execution: TestExecConfig
    fix: FixConfig
    deploy: DeployConfig
    webhook: WebhookConfig
    report_path: str


def load_config(path: str = "config.yaml") -> AppConfig:
    _load_env_file()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    b = raw["bamboo"]
    bamboo_auth_method = b.get("auth_method", "token")
    token = os.environ.get(b.get("token_env", "BAMBOO_TOKEN"))
    bamboo_username = os.environ.get(b.get("username_env", "BAMBOO_USERNAME"))
    bamboo_password = os.environ.get(b.get("password_env", "BAMBOO_PASSWORD"))

    s = raw["ssh"]
    password = None
    if s.get("auth_method") == "password":
        password = os.environ.get(s.get("password_env", "SSH_PASSWORD"))

    cfg = AppConfig(
        bamboo=BambooConfig(
            url=b["url"].rstrip("/"),
            auth_method=bamboo_auth_method,
            token=token,
            username=bamboo_username,
            password=bamboo_password,
            plans=[p["key"] for p in b.get("plans", [])],
            only_if_failed=b.get("only_if_failed", True),
        ),
        ssh=SshConfig(
            hosts=s["hosts"],
            username=s["username"],
            auth_method=s.get("auth_method", "agent"),
            key_path=os.path.expanduser(s.get("key_path", "~/.ssh/id_rsa")),
            password=password,
            jump_host=s.get("jump_host"),
        ),
        repo=RepoConfig(
            paths=[
                RepoPath(
                    name=p["name"],
                    local_path=p["local_path"],
                    container_base_path=p.get("container_base_path", ""),
                )
                for p in raw["repo"]["paths"]
            ]
        ),
        test_execution=TestExecConfig(
            remote_command=raw["test_execution"]["remote_command"],
            remote_results_dir=raw["test_execution"]["remote_results_dir"],
            local_results_dir=raw["test_execution"]["local_results_dir"],
        ),
        fix=FixConfig(
            auto_apply=raw["fix"].get("auto_apply", True),
            create_backup=raw["fix"].get("create_backup", True),
            max_iterations_per_plan=raw["fix"].get("max_iterations_per_plan", 2),
        ),
        deploy=DeployConfig(
            enabled=raw.get("deploy", {}).get("enabled", True),
            host_containers=raw.get("deploy", {}).get("host_containers", {}),
            remote_tmp_dir=raw.get("deploy", {}).get("remote_tmp_dir", "/tmp/bamboo-reg-agent-deploy"),
        ),
        webhook=WebhookConfig(
            enabled=raw.get("webhook", {}).get("enabled", False),
            host=raw.get("webhook", {}).get("host", "127.0.0.1"),
            port=raw.get("webhook", {}).get("port", 8787),
            token=os.environ.get(raw.get("webhook", {}).get("token_env", "WEBHOOK_TOKEN")),
        ),
        report_path=raw.get("reporting", {}).get("report_path", "./results/root_cause_report.md"),
    )

    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    problems = []
    if "TODO" in cfg.bamboo.url:
        problems.append("bamboo.url still contains a TODO placeholder")
    if cfg.bamboo.auth_method == "token" and not cfg.bamboo.token:
        problems.append(
            "Bamboo token not set. Export it, e.g.:  $env:BAMBOO_TOKEN='<token>'"
        )
    if cfg.bamboo.auth_method == "basic" and not (cfg.bamboo.username and cfg.bamboo.password):
        problems.append(
            "Bamboo basic auth selected but username/password env vars are not "
            "both set, e.g.:  $env:BAMBOO_USERNAME='<user>'; $env:BAMBOO_PASSWORD='<pass>'"
        )
    if any("TODO" in p for p in cfg.bamboo.plans):
        problems.append("bamboo.plans still contains TODO placeholder plan keys")
    if "TODO" in cfg.test_execution.remote_command:
        problems.append("test_execution.remote_command still contains a TODO placeholder")
    if cfg.ssh.auth_method == "password" and not cfg.ssh.password:
        problems.append(
            f"ssh.auth_method is 'password' but env var for password is not set"
        )
    if cfg.deploy.enabled and not cfg.deploy.host_containers:
        problems.append("deploy.enabled is true but deploy.host_containers is empty")
    if cfg.deploy.enabled and any(h not in cfg.deploy.host_containers for h in cfg.ssh.hosts):
        problems.append("deploy.host_containers is missing an entry for one or more ssh.hosts")
    if cfg.deploy.enabled:
        for r in cfg.repo.paths:
            if not r.container_base_path or "TODO" in r.container_base_path:
                problems.append(f"repo.paths[{r.name}].container_base_path is not set (still a TODO placeholder)")
    if cfg.webhook.enabled and not cfg.webhook.token:
        problems.append(
            "webhook.enabled is true but no shared secret is set; export the env var "
            "named in webhook.token_env before starting src/webhook_listener.py"
        )
    if problems:
        msg = "\n".join(f"  - {p}" for p in problems)
        raise SystemExit(
            "Configuration is incomplete. Fix config.yaml / env vars before running:\n" + msg
        )
