#!/usr/bin/env python3
"""Manage only this project's native PostgreSQL/NebulaGraph processes in WSL.

Invoked by windows_runtime.py as root. Credentials arrive on stdin, never argv.
Database files remain on WSL's Linux filesystem, not on an NTFS mount.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import urlopen


NEBULA_VERSION = "3.8.0"
NEBULA_ARCHIVE = f"nebula-graph-{NEBULA_VERSION}.ubuntu2004.amd64.tar.gz"
NEBULA_SHA256 = "dcc7a8cd58e70108431944025cff93212df7a12f4832f6c2cb66edb3576bb3d3"
NEBULA_URL = f"https://github.com/vesoft-inc/nebula/releases/download/v{NEBULA_VERSION}/{NEBULA_ARCHIVE}"
NEBULA_HOME = Path(f"/opt/credit-ontology-mvp/nebula-{NEBULA_VERSION}")
PG_BIN = Path("/usr/lib/postgresql/16/bin")
RUNTIME_ROOT = Path("/var/lib/credit-ontology-mvp")
SERVICE_USER = "credit-mvp"
PORTS = {"metad": 9559, "storaged": 9779}
HTTP_PORTS = {"metad": 19559, "storaged": 19779, "graphd": 19669}


class RuntimeFailure(RuntimeError):
    pass


def run(command, *, input=None, capture=False, check=True, timeout=600, cwd=None):
    result = subprocess.run(
        [str(item) for item in command], input=input, text=True, encoding="utf-8", cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None, timeout=timeout,
    )
    if check and result.returncode:
        # Do not echo stdin, which can contain database credentials.
        raise RuntimeFailure(f"Command failed: {command[0]} (exit {result.returncode})")
    return result


def validate_config(config):
    if not re.fullmatch(r"[a-f0-9]{12}", str(config.get("instance_id", ""))):
        raise RuntimeFailure("Invalid local instance ID")
    for name in ("pg_user", "pg_database"):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", str(config.get(name, ""))):
            raise RuntimeFailure(f"Invalid {name}")
    for name in ("pg_port", "graph_port"):
        port = config.get(name)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise RuntimeFailure(f"Invalid {name}")
    all_ports = [config["pg_port"], config["graph_port"], *PORTS.values(), *HTTP_PORTS.values()]
    if len(all_ports) != len(set(all_ports)):
        raise RuntimeFailure("Database ports must be distinct")
    if not isinstance(config.get("pg_password"), str) or not config["pg_password"]:
        raise RuntimeFailure("PostgreSQL password is required")
    return config


def owned_directory(path, user, mode=0o700):
    path.mkdir(parents=True, exist_ok=True)
    account = pwd.getpwnam(user)
    os.chown(path, account.pw_uid, account.pw_gid)
    path.chmod(mode)


def prepare_packages():
    if os.uname().machine != "x86_64":
        raise RuntimeFailure("The bundled NebulaGraph release requires x86_64 WSL")
    if not (PG_BIN / "initdb").exists():
        print("Installing PostgreSQL 16 from Ubuntu packages...", flush=True)
        os.environ["DEBIAN_FRONTEND"] = "noninteractive"
        run(["apt-get", "-o", "Acquire::Languages=none", "-o", "Acquire::Retries=2", "-o", "Acquire::http::Timeout=30", "update"])
        run(["apt-get", "install", "-y", "--no-install-recommends", "postgresql-16", "ca-certificates", "curl"])
    if not (NEBULA_HOME / "bin/nebula-graphd").exists():
        print("Downloading and verifying NebulaGraph 3.8.0...", flush=True)
        cache = Path("/var/cache/credit-ontology-mvp")
        cache.mkdir(parents=True, exist_ok=True)
        archive = cache / NEBULA_ARCHIVE
        if not archive.exists() or hashlib.sha256(archive.read_bytes()).hexdigest() != NEBULA_SHA256:
            partial = archive.with_suffix(".partial")
            run(["curl", "-fL", "--retry", "2", "--connect-timeout", "15", "--max-time", "600", "-o", partial, NEBULA_URL])
            if hashlib.sha256(partial.read_bytes()).hexdigest() != NEBULA_SHA256:
                partial.unlink(missing_ok=True)
                raise RuntimeFailure("NebulaGraph SHA-256 verification failed")
            partial.replace(archive)
        staging = cache / "unpacked"
        staging.mkdir(exist_ok=True)
        with tarfile.open(archive, "r:gz") as bundle:
            # The pinned archive contains only release files. Still constrain extraction.
            for member in bundle.getmembers():
                if not (staging / member.name).resolve().is_relative_to(staging.resolve()):
                    raise RuntimeFailure("Unsafe archive path")
                if member.issym() or member.islnk():
                    raise RuntimeFailure("Unexpected link in release archive")
            bundle.extractall(staging, filter="data")
        unpacked = staging / NEBULA_ARCHIVE.removesuffix(".tar.gz")
        NEBULA_HOME.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(unpacked), str(NEBULA_HOME))
    try:
        pwd.getpwnam(SERVICE_USER)
    except KeyError:
        run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", SERVICE_USER])


def base_path(config):
    return RUNTIME_ROOT / config["instance_id"]


def postgres_process_id(base):
    """A stale postmaster.pid must never authorize signalling a reused PID."""
    data = (base / "postgres").resolve()
    try:
        if not (data / "PG_VERSION").exists():
            return None
        lines = (data / "postmaster.pid").read_text().splitlines()
        pid = int(lines[0])
        if pid <= 1 or Path(lines[1]).resolve() != data:
            return None
        proc = Path(f"/proc/{pid}")
        if (proc / "exe").resolve(strict=True) != (PG_BIN / "postgres").resolve():
            return None
        if proc.stat().st_uid != pwd.getpwnam("postgres").pw_uid:
            return None
        arguments = (proc / "cmdline").read_bytes().split(b"\0")
        if not any(
            argument == b"-D" and index + 1 < len(arguments)
            and Path(os.fsdecode(arguments[index + 1])).resolve() == data
            for index, argument in enumerate(arguments)
        ):
            return None
        return pid
    except (OSError, ValueError, IndexError, KeyError):
        return None


def pg_status(base):
    return postgres_process_id(base) is not None


def stop_postgres(base):
    pid = postgres_process_id(base)
    if pid is None:
        return False
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return False
    try:
        # Recheck after opening the pidfd; the signal then targets that exact
        # process even if it exits and Linux reuses its numeric PID.
        if postgres_process_id(base) != pid:
            return False
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
        except ProcessLookupError:
            return False
        deadline = time.monotonic() + 45
        while postgres_process_id(base) == pid:
            if time.monotonic() >= deadline:
                raise RuntimeFailure("PostgreSQL did not stop gracefully; check WSL database logs")
            time.sleep(0.2)
        return True
    finally:
        os.close(descriptor)


def process_is_owned(base, service):
    pidfile = base / "nebula/pids" / f"nebula-{service}.pid"
    try:
        pid = int(pidfile.read_text().strip())
        if pid <= 1:
            return None
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        expected = str(base / "nebula/etc" / f"nebula-{service}.conf").encode()
        if executable != (NEBULA_HOME / "bin" / f"nebula-{service}").resolve():
            return None
        if not any(expected in item for item in command):
            return None
        return pid
    except (OSError, ValueError):
        return None


def assert_port_free(port):
    with socket.socket() as listener:
        # Allow an immediate restart after sockets enter TIME_WAIT.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeFailure(f"WSL port {port} is already in use by another process") from error


def write_nebula_configs(base, config):
    home = base / "nebula"
    for name in ("etc", "logs", "pids", "data/meta", "data/storage"):
        owned_directory(home / name, SERVICE_USER, 0o750)
    for service in ("metad", "storaged", "graphd"):
        overrides = {
            "daemonize": "true",
            "pid_file": str(home / "pids" / f"nebula-{service}.pid"),
            "log_dir": str(home / "logs"),
            "meta_server_addrs": "127.0.0.1:9559",
            "local_ip": "127.0.0.1",
            "port": str(config["graph_port"] if service == "graphd" else PORTS[service]),
            "ws_ip": "127.0.0.1",
            "ws_http_port": str(HTTP_PORTS[service]),
        }
        if service != "graphd":
            overrides["data_path"] = str(home / "data" / ("meta" if service == "metad" else "storage"))
        if service == "storaged":
            overrides.update(num_io_threads="2", num_worker_threads="4", cluster_id_path=str(home / "data/storage/cluster.id"))
        if service == "graphd":
            overrides.update(enable_authorize="true", num_netio_threads="2", num_worker_threads="4", listen_netdev="lo")
        template = (NEBULA_HOME / "etc" / f"nebula-{service}.conf.default").read_text()
        for key, value in overrides.items():
            line = f"--{key}={value}"
            pattern = rf"(?m)^--{re.escape(key)}=.*$"
            template = re.sub(pattern, lambda _match: line, template) if re.search(pattern, template) else template + "\n" + line + "\n"
        target = home / "etc" / f"nebula-{service}.conf"
        target.write_text(template)
        target.chmod(0o644)


def prepare(config):
    prepare_packages()
    base = base_path(config)
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o755)
    owned_directory(base / "postgres", "postgres")
    owned_directory(base / "socket", "postgres")
    owned_directory(base / "pg-logs", "postgres")
    if not (base / "postgres/PG_VERSION").exists():
        print("Initializing the project's isolated PostgreSQL cluster...", flush=True)
        descriptor, password_name = tempfile.mkstemp(prefix=".initdb-", dir=base)
        password_file = Path(password_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(config["pg_password"] + "\n")
            account = pwd.getpwnam("postgres")
            os.chown(password_file, account.pw_uid, account.pw_gid)
            run([
                "runuser", "-u", "postgres", "--", PG_BIN / "initdb", "-D", base / "postgres",
                "-U", config["pg_user"], "--encoding=UTF8", "--locale=C.UTF-8",
                "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--pwfile=" + str(password_file),
            ])
        finally:
            password_file.unlink(missing_ok=True)
    write_nebula_configs(base, config)
    return {"prepared": True, "data_directory": str(base)}


def stop(config, only=None):
    base = base_path(config)
    stopped = []
    names = set(only if only is not None else ("graphd", "storaged", "metad", "postgres"))
    for service in ("graphd", "storaged", "metad"):
        if service not in names:
            continue
        pid = process_is_owned(base, service)
        if pid:
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 40
            while process_is_owned(base, service) and time.monotonic() < deadline:
                time.sleep(0.2)
            if process_is_owned(base, service):
                raise RuntimeFailure(f"{service} did not stop gracefully; check WSL database logs")
            stopped.append(service)
    if "postgres" in names and stop_postgres(base):
        stopped.append("postgres")
    return {"stopped": stopped}


def start(config):
    prepare(config)
    base = base_path(config)
    newly_started = []
    try:
        if not pg_status(base):
            assert_port_free(config["pg_port"])
            run([
                "runuser", "-u", "postgres", "--", PG_BIN / "pg_ctl", "-D", base / "postgres",
                "-l", base / "pg-logs/server.log", "-w", "-t", "60", "start", "-o",
                f"-h 127.0.0.1 -p {config['pg_port']} -k {base / 'socket'}",
            ])
            newly_started.append("postgres")
        # Storage does not finish opening its HTTP/RPC ports until ADD HOSTS.
        # graphd must therefore be available before waiting for Storage readiness.
        for service in ("metad", "graphd", "storaged"):
            if process_is_owned(base, service):
                continue
            # Nebula's own PID check only tests kill(pid, 0). WSL can reuse the
            # PID after a reboot, so remove a stale file after our stronger check.
            (base / "nebula/pids" / f"nebula-{service}.pid").unlink(missing_ok=True)
            assert_port_free(config["graph_port"] if service == "graphd" else PORTS[service])
            assert_port_free(HTTP_PORTS[service])
            run([
                "runuser", "-u", SERVICE_USER, "--", NEBULA_HOME / "bin" / f"nebula-{service}",
                "--flagfile=" + str(base / "nebula/etc" / f"nebula-{service}.conf"),
            ], cwd=NEBULA_HOME)
            newly_started.append(service)
            if service == "storaged":
                continue
            deadline = time.monotonic() + 60
            while True:
                try:
                    with urlopen(f"http://127.0.0.1:{HTTP_PORTS[service]}/status", timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeFailure(f"{service} healthcheck timed out; logs: {base / 'nebula/logs'}")
                time.sleep(0.5)
        return {
            "started": newly_started, "data_directory": str(base),
            "graph_initialized": (base / "graph-initialized").exists(),
        }
    except BaseException:
        stop(config, newly_started)
        raise


def status(config):
    base = base_path(config)
    return {
        "postgres": pg_status(base),
        **{name: bool(process_is_owned(base, name)) for name in ("metad", "storaged", "graphd")},
        "data_directory": str(base),
    }


def main():
    if os.geteuid() != 0:
        raise RuntimeFailure("Run this helper through the CMD launcher, using WSL root")
    config = validate_config(json.load(sys.stdin))
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    with (RUNTIME_ROOT / (config["instance_id"] + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "prepare":
            result = prepare(config)
        elif action == "start":
            result = start(config)
        elif action == "stop":
            result = stop(config, config.get("only_stop"))
        elif action == "status":
            result = status(config)
        elif action == "graph-ready":
            (base_path(config) / "graph-initialized").touch()
            result = {"graph_initialized": True}
        else:
            raise RuntimeFailure("Unknown WSL runtime action")
    print("MVP_RUNTIME_JSON=" + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"WSL runtime error: {error}", file=sys.stderr, flush=True)
        sys.exit(1)
