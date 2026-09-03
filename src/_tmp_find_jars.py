"""Locate the e2net-core jar inside e2na, e2netio, scenario on dev10092."""
import sys
sys.path.insert(0, "src")
from config import load_config
from ssh_runner import SshRunner

cfg = load_config("config.yaml")
ssh = SshRunner(
    hosts=cfg.ssh.hosts,
    username=cfg.ssh.username,
    auth_method=cfg.ssh.auth_method,
    key_path=cfg.ssh.key_path,
    password=cfg.ssh.password,
    jump_host=cfg.ssh.jump_host,
)

host = "dev10092.dev.e2open.com"
containers = ["e2na", "e2netio", "scenario"]
for c in containers:
    cmd = f"docker exec {c} sh -c \"find /e2open/app -maxdepth 8 -iname '*e2net-core*.jar' 2>/dev/null\""
    code, out, err = ssh._exec_as_eoadmin(host, cmd, timeout=30)
    print(f"=== {c} (exit={code}) ===", flush=True)
    print(out.strip() or "(not found)", flush=True)
