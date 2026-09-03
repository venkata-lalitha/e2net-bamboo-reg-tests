"""Fetch e2net-core jar from a container on dev10092, patch it locally with the
fixed EventEnvelopeStoreService.class, push it back, and restart the container.
Usage: python src\\_tmp_patch_container.py <container_name> <jar_path_in_container>
"""
import sys
sys.path.insert(0, "src")
from config import load_config
from ssh_runner import SshRunner

container = sys.argv[1]
jar_in_container = sys.argv[2]

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
jump = cfg.ssh.jump_host
local_jar = f"_patch_{container}.jar"
remote_tmp = f"/tmp/bra_patch_{container}.jar"

# 1. docker cp OUT of the container to a tmp path on dev10092 itself.
code, out, err = ssh._exec_as_eoadmin(host, f"docker cp {container}:{jar_in_container} {remote_tmp}", timeout=60)
print(f"[{container}] docker cp OUT exit={code}")
if code != 0:
    raise SystemExit(f"docker cp OUT failed: {err}")

# 2. scp it back to the jump host (as eoadmin), then chmod so vlalitha can read it.
if host != jump:
    code, out, err = ssh._exec_as_eoadmin(jump, f"scp -o StrictHostKeyChecking=no {host}:{remote_tmp} {remote_tmp}")
    print(f"[{container}] scp to jump exit={code}")
    if code != 0:
        raise SystemExit(f"scp to jump failed: {err}")
    code, out, err = ssh._exec_as_eoadmin(jump, f"chmod 644 {remote_tmp}")
    print(f"[{container}] chmod on jump exit={code}")

# 3. sftp download from jump host to local disk.
client = ssh._connect(jump)
try:
    sftp = client.open_sftp()
    sftp.get(remote_tmp, local_jar)
    sftp.close()
finally:
    client.close()
print(f"[{container}] downloaded to {local_jar}")
