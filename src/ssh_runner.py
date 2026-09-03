"""SSH execution + result retrieval on the Linux hosts running E2net."""
from __future__ import annotations

import posixpath
import stat
import paramiko


class SshRunner:
    def __init__(self, hosts, username, auth_method, key_path=None, password=None, jump_host=None):
        self.hosts = hosts
        self.username = username
        self.auth_method = auth_method
        self.key_path = key_path
        self.password = password
        # Host that's directly reachable with the above credentials, used to
        # hop (via eoadmin's passwordless SSH trust) to hosts that reject
        # self.username/password directly (e.g. dev10092's AD auth).
        self.jump_host = jump_host
        self._host_index = 0

    def _next_host(self) -> str:
        host = self.hosts[self._host_index % len(self.hosts)]
        self._host_index += 1
        return host

    @staticmethod
    def _as_eoadmin(command: str) -> str:
        """Wraps command to run as the eoadmin account (confirmed passwordless
        via `su - eoadmin -c`), since only eoadmin owns the log files /
        has the docker group membership needed for docker cp/restart.
        """
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        return f'su - eoadmin -c "{escaped}"'

    def _exec_as_eoadmin(self, target_host: str, command: str, timeout: int = 60):
        """Runs command as eoadmin on target_host. If target_host isn't the
        jump_host, hops there first (as eoadmin, via its passwordless SSH
        trust) since target_host may reject self.username/password directly.
        Returns (exit_code, stdout, stderr).
        """
        connect_host = self.jump_host or target_host
        client = self._connect(connect_host)
        try:
            if not self.jump_host or target_host == self.jump_host:
                full_command = self._as_eoadmin(command)
            else:
                inner = command.replace("'", "'\\''")
                hop = f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no {target_host} '{inner}'"
                full_command = self._as_eoadmin(hop)
            stdin, stdout, stderr = client.exec_command(full_command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return exit_code, out, err
        finally:
            client.close()

    def _connect(self, host: str) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {"hostname": host, "username": self.username, "timeout": 30}
        if self.auth_method == "key":
            kwargs["key_filename"] = self.key_path
        elif self.auth_method == "password":
            kwargs["password"] = self.password
        # "agent" -> paramiko will try ssh-agent / default keys automatically
        client.connect(**kwargs)
        return client

    def run_plan(self, plan_key: str, command_template: str, host: str | None = None, test_suite: str | None = None):
        """Runs the regression command for a plan on a host (round-robin if
        host is not specified). {host} and {test_suite} in command_template
        are substituted alongside {plan_key}. Returns (host, exit_code, stdout, stderr).
        """
        host = host or self._next_host()
        command = command_template.format(plan_key=plan_key, host=host, test_suite=test_suite or "")
        client = self._connect(host)
        try:
            stdin, stdout, stderr = client.exec_command(command, timeout=3600)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return host, exit_code, out, err
        finally:
            client.close()

    def fetch_results(self, host: str, remote_dir: str, local_dir: str) -> list:
        """Recursively downloads *.xml JUnit files from remote_dir to local_dir.
        Returns list of local file paths downloaded.
        """
        import os

        os.makedirs(local_dir, exist_ok=True)
        client = self._connect(host)
        downloaded = []
        try:
            sftp = client.open_sftp()
            try:
                self._download_dir(sftp, remote_dir, local_dir, downloaded)
            finally:
                sftp.close()
        finally:
            client.close()
        return downloaded

    def deploy_file(self, host: str, local_path: str, remote_tmp_dir: str, container_name: str, container_dest_path: str) -> None:
        """Uploads local_path to remote_tmp_dir (via SFTP to jump_host if host
        itself rejects self.username/password, then eoadmin scp-hops it to
        host), then `docker cp`s it into container_name at
        container_dest_path as eoadmin. Raises RuntimeError on failure.
        """
        import os

        remote_tmp_path = posixpath.join(remote_tmp_dir, os.path.basename(local_path))
        stage_host = self.jump_host or host
        client = self._connect(stage_host)
        try:
            sftp = client.open_sftp()
            try:
                client.exec_command(f"mkdir -p {remote_tmp_dir}")[1].channel.recv_exit_status()
                sftp.put(local_path, remote_tmp_path)
            finally:
                sftp.close()
            # SFTP uploads default to 0600 owned by self.username, which
            # eoadmin can't read when scp-hopping below - open it up.
            client.exec_command(f"chmod 644 {remote_tmp_path}")[1].channel.recv_exit_status()
        finally:
            client.close()

        if self.jump_host and host != self.jump_host:
            exit_code, out, err = self._exec_as_eoadmin(
                self.jump_host,
                f"scp -o StrictHostKeyChecking=no {remote_tmp_path} {host}:{remote_tmp_path}",
            )
            if exit_code != 0:
                raise RuntimeError(f"scp hop to {host} failed (exit {exit_code}): {err}")

        exit_code, out, err = self._exec_as_eoadmin(
            host, f"docker cp {remote_tmp_path} {container_name}:{container_dest_path}", timeout=60
        )
        if exit_code != 0:
            raise RuntimeError(f"docker cp failed on {host} (exit {exit_code}): {err}")

    def restart_container(self, host: str, container_name: str) -> None:
        """Restarts container_name on host via `docker restart` (as eoadmin,
        hopping through jump_host if host rejects self.username/password
        directly), so a fix just docker cp'd in actually takes effect.
        Raises RuntimeError on failure.
        """
        exit_code, out, err = self._exec_as_eoadmin(host, f"docker restart {container_name}", timeout=120)
        if exit_code != 0:
            raise RuntimeError(f"docker restart failed on {host} (exit {exit_code}): {err}")

    def _download_dir(self, sftp, remote_dir, local_dir, downloaded):
        import os

        try:
            entries = sftp.listdir_attr(remote_dir)
        except FileNotFoundError:
            return
        for entry in entries:
            remote_path = posixpath.join(remote_dir, entry.filename)
            local_path = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode):
                os.makedirs(local_path, exist_ok=True)
                self._download_dir(sftp, remote_path, local_path, downloaded)
            elif entry.filename.endswith(".xml"):
                sftp.get(remote_path, local_path)
                downloaded.append(local_path)
