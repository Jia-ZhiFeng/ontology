"""One-command Windows runtime: WSL databases and native Python servers.

The launcher uses only the standard library until it has prepared .venv.
It never starts Docker, modifies .env, or stops an unrelated process.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import venv
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
LOCAL_ENV = ROOT / ".env.local"
STATE_FILE = RUNTIME / "state.json"
VENV_PYTHON = ROOT / ".venv/Scripts/python.exe"
API_BASE = "http://127.0.0.1:8000/api"
FRONTEND_BASE = "http://127.0.0.1:8080"
BUSINESS_TABLES = {
    "natural_person", "dealer", "vehicle", "loan_application", "credit_report",
    "external_debt", "risk_assessment_report", "anti_fraud_report", "phone_verification_record",
}


class RuntimeFailure(RuntimeError):
    pass


def say(message):
    print(message, flush=True)


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_state():
    if not STATE_FILE.exists():
        return {"services": {}}
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not isinstance(state.get("services"), dict):
            raise ValueError()
        return state
    except (ValueError, OSError) as error:
        raise RuntimeFailure("运行状态文件损坏；请检查 .runtime/state.json，启动器不会猜测并终止进程。") from error


@contextmanager
def runtime_lock():
    import msvcrt
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "launcher.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeFailure("另一个启动/停止操作正在运行，请等待它完成。") from error
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def ensure_venv(arguments):
    if sys.version_info < (3, 10):
        raise RuntimeFailure("需要 Python 3.10 或更新版本。")
    if Path(sys.executable).resolve() != VENV_PYTHON.resolve():
        if not VENV_PYTHON.exists():
            say("[1/6] 创建项目 Python 虚拟环境...")
            venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
        return subprocess.call([str(VENV_PYTHON), str(Path(__file__)), *arguments], cwd=ROOT)
    return None


def ensure_dependencies():
    requirements = ROOT / "backend/requirements.txt"
    missing = []
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[[^]]+\])?==(.+)", line.strip())
        if not match:
            continue
        try:
            installed = importlib.metadata.version(match[1])
        except importlib.metadata.PackageNotFoundError:
            installed = None
        if installed != match[2]:
            missing.append(match[1])
    if missing:
        say("[1/6] 安装后端 Python 依赖（首次运行需要网络）...")
        result = subprocess.run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
            "-r", str(requirements),
        ], cwd=ROOT)
        if result.returncode:
            raise RuntimeFailure("Python 依赖安装失败，请检查网络后重新运行 start.cmd。")


def dotenv_values_at(path):
    from dotenv import dotenv_values
    return {key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None}


def generate_local_config():
    if LOCAL_ENV.exists():
        return
    source = ROOT / ".env"
    values = dotenv_values_at(source if source.exists() else ROOT / ".env.example")
    values.update({
        "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "5433",
        "NEBULA_HOST": "127.0.0.1", "NEBULA_USER": "root",
        "MVP_AUTH_MODE": "local_session", "MVP_LOCAL_SESSION_TRUST_PROXY": "false",
        "WORKBENCH_UNAUTHENTICATED": "false", "MVP_LOCAL_SESSION_COOKIE_SECURE": "false",
        "CORS_ORIGINS": "http://localhost:8080,http://127.0.0.1:8080",
        "MVP_LOCAL_SESSION_ALLOWED_ORIGINS": "http://localhost:8080,http://127.0.0.1:8080",
        "MVP_LOCAL_INSTANCE_ID": hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:12],
        "MVP_WSL_DISTRO": "Ubuntu-24.04",
    })
    for key, defaults in {
        "MVP_API_KEY": {"", "replace-with-a-long-random-token"},
        "POSTGRES_PASSWORD": {"", "credit123"},
        "NEBULA_PASSWORD": {"", "nebula"},
        "MVP_LOCAL_SESSION_SECRET": {""},
    }.items():
        if values.get(key, "") in defaults or (key == "MVP_API_KEY" and len(values.get(key, "")) < 32):
            values[key] = secrets.token_urlsafe(18 if key == "NEBULA_PASSWORD" else 36)
    text = "# Generated native Windows + WSL runtime. Original .env is unchanged.\n"
    text += "# Stop with stop.cmd before changing ports, credentials or WSL distro.\n"
    # dotenv single-quoted values avoid interpolation and preserve Unicode roles.
    for key, value in values.items():
        if any(character in value for character in "\r\n\0"):
            raise RuntimeFailure(f"配置 {key} 不支持多行值，请先检查 .env。")
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        text += f"{key}='{escaped}'\n"
    # Exclusive creation: never replace a user's local configuration.
    try:
        with LOCAL_ENV.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except FileExistsError:
        pass
    say("[2/6] 已生成 .env.local；原 .env 保持不变。")


def load_local_config(*, validate=True):
    if not LOCAL_ENV.exists():
        raise RuntimeFailure("尚未初始化本地运行环境，请先运行 start.cmd。")
    values = dotenv_values_at(ROOT / ".env.example")
    values.update(dotenv_values_at(LOCAL_ENV))
    if not validate:
        return values
    if any(values.get(key) not in {"127.0.0.1", "localhost"} for key in ("POSTGRES_HOST", "NEBULA_HOST")):
        raise RuntimeFailure("本地启动器只管理本机数据库；请检查 .env.local 中的数据库 Host。")
    if values.get("MVP_AUTH_MODE") != "local_session":
        raise RuntimeFailure("一键工作台需要 MVP_AUTH_MODE=local_session。")
    if values.get("MVP_LOCAL_SESSION_TRUST_PROXY", "").lower() != "false":
        raise RuntimeFailure("本机直接运行时请设置 MVP_LOCAL_SESSION_TRUST_PROXY=false。")
    if len(values.get("MVP_LOCAL_SESSION_SECRET", "").encode()) < 32:
        raise RuntimeFailure(".env.local 中的本机会话签名密钥至少需要 32 字节。")
    if values.get("NEBULA_USER") != "root":
        raise RuntimeFailure("本地 NebulaGraph 初始化需要 NEBULA_USER=root。")
    if len(values.get("NEBULA_PASSWORD", "")) > 24:
        raise RuntimeFailure("NebulaGraph 3.8 密码最长 24 个字符，请检查 .env.local。")
    for key in ("POSTGRES_PASSWORD", "NEBULA_PASSWORD"):
        if not values.get(key) or any(c in values[key] for c in "\r\n\0"):
            raise RuntimeFailure(f".env.local 中 {key} 必须是非空单行值。")
    return values


def wsl_config(values):
    try:
        return {
            "instance_id": values["MVP_LOCAL_INSTANCE_ID"],
            "pg_user": values["POSTGRES_USER"], "pg_database": values["POSTGRES_DATABASE"],
            "pg_password": values["POSTGRES_PASSWORD"], "pg_port": int(values["POSTGRES_PORT"]),
            "graph_port": int(values["NEBULA_PORT"]),
        }
    except (KeyError, ValueError) as error:
        raise RuntimeFailure(".env.local 中缺少有效的本地数据库配置。") from error


def decode_wsl_output(raw):
    if b"\0" in raw:
        return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return raw.decode("utf-8", errors="replace")


def installed_distros():
    if not shutil.which("wsl.exe"):
        raise RuntimeFailure("未安装 WSL2。请先在管理员终端执行：wsl --install -d Ubuntu-24.04")
    result = subprocess.run(["wsl.exe", "--list", "--quiet"], capture_output=True)
    if result.returncode:
        raise RuntimeFailure("WSL 不可用，请检查 Windows 的 WSL2 功能。")
    return [line.strip() for line in decode_wsl_output(result.stdout).splitlines() if line.strip()]


def call_wsl(action, values, *, only_stop=None):
    distro = values["MVP_WSL_DISTRO"]
    if distro not in installed_distros() or distro.casefold().startswith("docker"):
        raise RuntimeFailure(f"未找到独立 Ubuntu 发行版 {distro}。请安装 Ubuntu-24.04，或修改 .env.local 的 MVP_WSL_DISTRO。")
    prefix = ["wsl.exe", "--distribution", distro, "--user", "root", "--exec"]
    helper = ROOT / "scripts/wsl_runtime.py"
    converted = subprocess.run([*prefix, "wslpath", "-a", "-u", str(helper)], capture_output=True, timeout=30)
    if converted.returncode:
        raise RuntimeFailure("WSL 无法访问项目目录，请检查磁盘挂载。")
    linux_path = decode_wsl_output(converted.stdout).strip()
    config = wsl_config(values)
    if only_stop is not None:
        config["only_stop"] = only_stop
    completed = subprocess.run(
        [*prefix, "python3", linux_path, action], input=json.dumps(config).encode(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1500,
    )
    output = decode_wsl_output(completed.stdout)
    for key in ("POSTGRES_PASSWORD", "NEBULA_PASSWORD", "MVP_API_KEY", "MVP_LOCAL_SESSION_SECRET"):
        if values.get(key):
            output = output.replace(values[key], "[redacted]")
    (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "logs/wsl-setup.log").open("a", encoding="utf-8") as log:
        log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} {action}\n{output}\n")
    if completed.returncode:
        last = output.strip().splitlines()[-1] if output.strip() else "WSL command failed"
        raise RuntimeFailure(f"WSL 数据库操作失败：{last}\n详细日志：{RUNTIME / 'logs/wsl-setup.log'}")
    for line in reversed(output.splitlines()):
        if line.startswith("MVP_RUNTIME_JSON="):
            return json.loads(line.partition("=")[2])
    raise RuntimeFailure("WSL 数据库助手未返回有效的运行状态。")


def api(path, values, *, body=None, method=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(API_BASE + path, data=data, method=method, headers={
        "X-API-Key": values["MVP_API_KEY"], "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        # Business/API details can contain sensitive values. Leave them server-side.
        from app.core.security import mask_payload
        diagnostic = json.dumps(mask_payload(error.read().decode("utf-8", errors="replace")), ensure_ascii=False)
        for key in ("POSTGRES_PASSWORD", "NEBULA_PASSWORD", "MVP_API_KEY", "MVP_LOCAL_SESSION_SECRET"):
            if values.get(key):
                diagnostic = diagnostic.replace(values[key], "[redacted]")
        with (RUNTIME / "logs/launcher.log").open("a", encoding="utf-8") as log:
            log.write(f"API {path} HTTP {error.code}: {diagnostic}\n")
        raise RuntimeFailure(f"本地后端接口 {path} 返回 HTTP {error.code}，请查看 backend.log。") from error


def health(url):
    try:
        with urlopen(url, timeout=3) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def bootstrap_database(values):
    import psycopg
    from psycopg import sql
    from app.postgres.client import PostgresService
    from app.action.execution_repository import PostgresActionExecutionRepository
    from app.action.workflow import PostgresPersonalCreditWorkflowRepository
    cfg = dict(host=values["POSTGRES_HOST"], port=int(values["POSTGRES_PORT"]),
               user=values["POSTGRES_USER"], password=values["POSTGRES_PASSWORD"], connect_timeout=5)
    database = values["POSTGRES_DATABASE"]
    with psycopg.connect(**cfg, dbname="postgres", autocommit=True) as connection:
        if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    with psycopg.connect(**cfg, dbname=database) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"
        )}
        if not tables:
            connection.execute((ROOT / "postgres/credit_ontology_seed.sql").read_text(encoding="utf-8"), prepare=False)
        elif not BUSINESS_TABLES.issubset(tables):
            raise RuntimeFailure("本地数据库存在不完整业务结构；启动器不会覆盖数据，请先检查或恢复备份。")
        connection.execute("CREATE SCHEMA IF NOT EXISTS mvp_control")
        connection.execute("""CREATE TABLE IF NOT EXISTS mvp_control.local_runtime_migrations (
            name text PRIMARY KEY, sha256 char(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now()
        )""")
    postgres = PostgresService(cfg["host"], cfg["port"], database, cfg["user"], cfg["password"])
    PostgresActionExecutionRepository(postgres).ensure_schema()
    PostgresPersonalCreditWorkflowRepository(postgres).ensure_schema()
    # Business test datasets 003/004/006 remain explicit opt-in data imports.
    for name in ("002_add_external_debt_monthly_payment.sql", "005_add_workflow_input_summary.sql"):
        migration = ROOT / "postgres/migrations" / name
        checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
        with psycopg.connect(**cfg, dbname=database, autocommit=True) as connection:
            previous = connection.execute("SELECT sha256 FROM mvp_control.local_runtime_migrations WHERE name=%s", (name,)).fetchone()
            if previous:
                if previous[0] != checksum:
                    raise RuntimeFailure(f"已执行的迁移 {name} 内容发生变化，请新增迁移。")
                continue
            # Existing SQL files contain their own BEGIN/COMMIT statements.
            connection.execute(migration.read_text(encoding="utf-8"), prepare=False)
            connection.execute("INSERT INTO mvp_control.local_runtime_migrations(name,sha256) VALUES (%s,%s)", (name, checksum))


def prepare_graph(values, initialized):
    from nebula3.Config import Config
    from nebula3.gclient.net import ConnectionPool
    from app.kg.nebula import NebulaGraphService
    pool = ConnectionPool()
    configuration = Config()
    configuration.timeout = 5000
    if not pool.init([(values["NEBULA_HOST"], int(values["NEBULA_PORT"]))], configuration):
        raise RuntimeFailure("无法连接本地 NebulaGraph。")
    try:
        session = None
        try:
            session = pool.get_session("root", values["NEBULA_PASSWORD"])
        except Exception:
            if initialized:
                raise RuntimeFailure("本地 NebulaGraph 密码不匹配；启动器不会重置已有图数据库密码。")
            session = pool.get_session("root", "nebula")
            result = session.execute("ALTER USER root WITH PASSWORD " + json.dumps(values["NEBULA_PASSWORD"]))
            if not result.is_succeeded():
                raise RuntimeFailure("无法设置新图数据库的本地凭据。")
        try:
            result = session.execute("SHOW HOSTS")
            if not result.is_succeeded():
                raise RuntimeFailure("无法读取 NebulaGraph Storage 状态。")
            hosts = [result.row_values(i) for i in range(result.row_size())]
            present = any(str(row[0].cast()) == "127.0.0.1" and int(row[1].cast()) == 9779 for row in hosts)
            if not present:
                result = session.execute('ADD HOSTS "127.0.0.1":9779')
                if not result.is_succeeded():
                    raise RuntimeFailure("无法注册本地 NebulaGraph Storage。")
            deadline = time.monotonic() + 120
            while True:
                result = session.execute("SHOW HOSTS")
                if result.is_succeeded() and any(
                    str((row := result.row_values(i))[0].cast()) == "127.0.0.1"
                    and int(row[1].cast()) == 9779 and str(row[2].cast()).upper() == "ONLINE"
                    for i in range(result.row_size())
                ):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeFailure("NebulaGraph Storage 未在 120 秒内 ONLINE。")
                time.sleep(1)
        finally:
            if session is not None:
                session.release()
    finally:
        pool.close()
    deadline = time.monotonic() + 120
    while True:
        try:
            with urlopen("http://127.0.0.1:19779/status", timeout=3) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeFailure("NebulaGraph Storage 已注册，但 HTTP 就绪检查超时。")
        time.sleep(0.5)
    graph = NebulaGraphService(values["NEBULA_HOST"], int(values["NEBULA_PORT"]), "root", values["NEBULA_PASSWORD"], values["NEBULA_SPACE"])
    try:
        graph.ensure_schema()
        wait_for_graph_schema(graph)
    finally:
        graph.close()
    call_wsl("graph-ready", values)


def wait_for_graph_schema(graph, timeout=30):
    # Nebula schema changes are asynchronous. The space can be visible while
    # its freshly-created tags/edge are still missing from graphd's cache.
    deadline = time.monotonic() + timeout
    while True:
        try:
            graph.snapshot(1)
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def process_stamp(pid):
    """Windows kernel creation time prevents stale state from matching a reused PID."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def service_owned(record):
    return bool(record and record.get("stamp") and process_stamp(record.get("pid", 0)) == record["stamp"])


def port_free(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def service_health(role, values):
    if role == "graph-bridge":
        try:
            with socket.create_connection(("127.0.0.1", int(values["NEBULA_PORT"])), timeout=1):
                return True
        except OSError:
            return False
    result = health(API_BASE + "/health" if role == "backend" else FRONTEND_BASE + "/_runtime/health")
    return bool(result and result.get("status") == "ok" and (role == "backend" or result.get("instance") == values["MVP_LOCAL_INSTANCE_ID"]))


def configuration_digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def start_wsl_keeper(values, state):
    if service_owned(state["services"].get("wsl")):
        return
    distro = values["MVP_WSL_DISTRO"]
    if distro not in installed_distros() or distro.casefold().startswith("docker"):
        raise RuntimeFailure("请先安装独立的 Ubuntu-24.04 WSL2 发行版。")
    stop_file = RUNTIME / ("wsl-" + secrets.token_hex(12) + ".stop")
    prefix = ["wsl.exe", "--distribution", distro, "--exec"]
    converted = subprocess.run([*prefix, "wslpath", "-a", "-u", str(stop_file)], capture_output=True, timeout=30)
    if converted.returncode:
        raise RuntimeFailure("无法访问 WSL 项目目录。")
    script = "import pathlib,sys,time\nstop=pathlib.Path(sys.argv[1])\nwhile not stop.exists(): time.sleep(0.5)"
    process = subprocess.Popen(
        [*prefix, "python3", "-c", script, decode_wsl_output(converted.stdout).strip()],
        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
    )
    stamp = process_stamp(process.pid)
    if not stamp:
        raise RuntimeFailure("WSL 保活进程未能启动。")
    state["services"]["wsl"] = {"pid": process.pid, "stamp": stamp, "stop_file": str(stop_file)}
    save_json(STATE_FILE, state)


def start_server(role, values, state):
    record = state["services"].get(role)
    if service_owned(record):
        if service_health(role, values):
            return False
        raise RuntimeFailure(f"已有 {role} 进程尚未就绪，请先运行 stop.cmd 并检查日志。")
    port = int(values["NEBULA_PORT"]) if role == "graph-bridge" else 8000 if role == "backend" else 8080
    if not port_free(port):
        raise RuntimeFailure(f"端口 {port} 被其他进程占用；启动器不会结束该进程。")
    nonce = secrets.token_hex(12)
    stop_file = RUNTIME / f"{role}-{nonce}.stop"
    environment = os.environ.copy()
    environment.update(values)
    environment.update(PYTHONPATH=str(ROOT / "backend"), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "logs" / f"{role}.log").open("ab", buffering=0) as log:
        process = subprocess.Popen([
            str(VENV_PYTHON), "-u", str(ROOT / "scripts/windows_service.py"), role,
            "--port", str(port), "--stop-file", str(stop_file),
            "--instance", values["MVP_LOCAL_INSTANCE_ID"],
        ], cwd=ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
    stamp = process_stamp(process.pid)
    if not stamp:
        raise RuntimeFailure(f"{role} 进程未能启动，请查看 .runtime/logs/{role}.log。")
    state["services"][role] = {"pid": process.pid, "stamp": stamp, "stop_file": str(stop_file)}
    save_json(STATE_FILE, state)
    deadline = time.monotonic() + 60
    while not service_health(role, values):
        if process.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeFailure(f"{role} 未能就绪，请查看 .runtime/logs/{role}.log。")
        time.sleep(0.5)
    return True


def stop_server(role, state):
    record = state["services"].get(role)
    if not record:
        return
    if service_owned(record):
        stop_file = Path(record["stop_file"]).resolve()
        if stop_file.parent != RUNTIME.resolve() or not re.fullmatch(rf"{role}-[a-f0-9]{{24}}\.stop", stop_file.name):
            raise RuntimeFailure("停止标记路径无效，拒绝操作。")
        stop_file.touch()
        deadline = time.monotonic() + 45
        while service_owned(record) and time.monotonic() < deadline:
            time.sleep(0.25)
        if service_owned(record):
            say(f"{role} 正常退出超时，将结束已核实的项目进程。")
            subprocess.run(["taskkill.exe", "/PID", str(record["pid"]), "/T", "/F"], capture_output=True, timeout=15)
            deadline = time.monotonic() + 10
            while service_owned(record) and time.monotonic() < deadline:
                time.sleep(0.2)
        if service_owned(record):
            raise RuntimeFailure(f"无法停止 {role}，已保留运行状态。")
        stop_file.unlink(missing_ok=True)
    state["services"].pop(role, None)


def bootstrap_ontology(values):
    from app.ontology.baseline import build_personal_credit_baseline
    version = build_personal_credit_baseline().version
    versions = api("/ontology/versions", values)
    if not versions.get("active_version"):
        stats = api("/kg/stats", values)
        records = versions.get("versions", [])
        if not (len(records) == 1 and records[0]["version"] == version
                and records[0]["status"] == "approved" and stats.get("vertices") == 0):
            raise RuntimeFailure("本地已有非初始版本或图数据，请在本体后台确认并发布活动版本。")
        say("首次初始化：通过既有发布门禁发布确定性本体基线...")
        api(f"/ontology/versions/{version}/publish", values, method="POST", timeout=240)
    if not api("/kg/mapping/report", values).get("valid"):
        raise RuntimeFailure("图谱映射检查未通过，已有数据已保留，请检查后端与发布记录。")


def ensure_graph_transport(values, state):
    # nebula3-python resolves hosts using gethostbyname (IPv4). Some WSL2 hosts
    # forward a dual-stack Thrift listener only to Windows ::1. Bridge loopback
    # IPv4 to loopback IPv6 only when the normal IPv4 route is unavailable.
    if service_health("graph-bridge", values):
        return
    try:
        with socket.create_connection(("::1", int(values["NEBULA_PORT"])), timeout=3):
            pass
    except OSError as error:
        raise RuntimeFailure("WSL NebulaGraph 尚未提供本机 TCP 连接。") from error
    say("启用 NebulaGraph 本机 IPv4 / IPv6 连接桥接...")
    start_server("graph-bridge", values, state)


def start(values, *, open_browser):
    state = read_state()
    instance = values["MVP_LOCAL_INSTANCE_ID"]
    if state.get("instance_id") not in (None, instance):
        raise RuntimeFailure("实例 ID 与运行记录不同；请恢复原配置后先执行 stop.cmd。")
    if state.get("configuration_digest") not in (None, configuration_digest(values)) and any(service_owned(record) for record in state["services"].values()):
        raise RuntimeFailure("运行期间配置发生变化，请先执行 stop.cmd，再运行 start.cmd。")
    for role, port in (("backend", 8000), ("frontend", 8080)):
        if not service_owned(state["services"].get(role)) and not port_free(port):
            raise RuntimeFailure(f"端口 {port} 被其他进程占用，请先释放端口。")
    state.update(instance_id=instance, distro=values["MVP_WSL_DISTRO"], configuration_digest=configuration_digest(values))
    state["runtime_modes"] = {key: values.get(key, "disabled") for key in (
        "CREDIT_PROVIDER_MODE", "CREDIT_CONSENT_MODE", "FRAUD_PROVIDER_MODE", "PHONE_TASK_DISPATCH_MODE",
    )}
    save_json(STATE_FILE, state)
    new_databases = []
    original_roles = {role for role, record in state["services"].items() if service_owned(record)}
    try:
        start_wsl_keeper(values, state)
        say("[3/6] 准备并启动 WSL PostgreSQL / NebulaGraph（首次需要联网下载）...")
        databases = call_wsl("start", values)
        new_databases = databases.get("started", [])
        ensure_graph_transport(values, state)
        say("[4/6] 检查数据库、应用结构迁移并等待 Storage ONLINE...")
        bootstrap_database(values)
        prepare_graph(values, databases.get("graph_initialized", False))
        say("[5/6] 启动 Windows 后端并校验已发布本体...")
        start_server("backend", values, state)
        bootstrap_ontology(values)
        say("[6/6] 启动页面服务并完成健康检查...")
        start_server("frontend", values, state)
        save_json(STATE_FILE, state)
        say("\n启动完成。")
        say("信审工作台：" + FRONTEND_BASE + "/credit-review.html")
        say("本体建模后台：" + FRONTEND_BASE)
        say("API 文档：http://127.0.0.1:8000/docs")
        say("停止：stop.cmd；检查状态：status.cmd")
        say("日志：" + str(RUNTIME / "logs"))
        if open_browser:
            webbrowser.open(FRONTEND_BASE + "/credit-review.html")
    except BaseException:
        for role in reversed(list(state["services"])):
            if role not in original_roles and role != "wsl":
                try:
                    stop_server(role, state)
                except Exception:
                    say(f"自动清理 {role} 未完成，请运行 stop.cmd。")
        save_json(STATE_FILE, state)
        if new_databases:
            try:
                call_wsl("stop", values, only_stop=new_databases)
            except Exception:
                say("自动停止本轮数据库未完成，请运行 stop.cmd。")
        if "wsl" not in original_roles:
            stop_server("wsl", state)
            save_json(STATE_FILE, state)
        raise


def stop(values):
    state = read_state()
    failures = []
    for role in ("frontend", "backend", "graph-bridge"):
        try:
            stop_server(role, state)
        except Exception as error:
            failures.append(str(error))
    save_json(STATE_FILE, state)
    if failures:
        raise RuntimeFailure("；".join(failures))
    target = dict(values)
    if state.get("instance_id"):
        target["MVP_LOCAL_INSTANCE_ID"] = state["instance_id"]
        target["MVP_WSL_DISTRO"] = state["distro"]
    call_wsl("stop", target)
    stop_server("wsl", state)
    save_json(STATE_FILE, state)
    say("本项目的页面、后端和数据库已停止。数据已保留，其他 WSL 应用继续运行。")


def status(values):
    state = read_state()
    result = call_wsl("status", values)
    result["backend"] = service_owned(state["services"].get("backend")) and service_health("backend", values)
    result["frontend"] = service_owned(state["services"].get("frontend")) and service_health("frontend", values)
    for name in ("postgres", "metad", "storaged", "graphd", "backend", "frontend"):
        say(f"{name:12} {'RUNNING' if result[name] else 'STOPPED / NOT READY'}")
    for name in ("wsl", "graph-bridge"):
        if name in state["services"]:
            say(f"{name:12} {'RUNNING' if service_owned(state['services'][name]) else 'STOPPED'}")
    say("WSL 数据目录：" + result["data_directory"])
    say("日志目录：" + str(RUNTIME / "logs"))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "status", "setup"))
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    arguments = parser.parse_args()
    if os.name != "nt":
        parser.error("Please run this launcher on Windows.")
    try:
        if arguments.command in {"start", "setup"}:
            delegated = ensure_venv(sys.argv[1:])
            if delegated is not None:
                return delegated
        with runtime_lock():
            if arguments.command in {"start", "setup"}:
                ensure_dependencies()
                generate_local_config()
            if not LOCAL_ENV.exists():
                say("本地运行环境尚未初始化，没有启动器管理的服务。")
                return 0
            values = load_local_config(validate=arguments.command in {"start", "setup"})
            os.environ.update(values)
            sys.path.insert(0, str(ROOT / "backend"))
            if arguments.command == "start":
                start(values, open_browser=not arguments.no_browser)
            elif arguments.command == "stop":
                stop(values)
            elif arguments.command == "status":
                status(values)
            else:
                say("准备 Python 和 WSL 数据库依赖...")
                result = call_wsl("prepare", values)
                say("依赖准备完成。运行 start.cmd 即可启动。")
                say("WSL 数据目录：" + result["data_directory"])
        return 0
    except KeyboardInterrupt:
        say("操作已中断；可运行 stop.cmd 停止本项目服务。")
        return 130
    except Exception as error:
        (RUNTIME / "logs").mkdir(parents=True, exist_ok=True)
        diagnostic = traceback.format_exc()
        for key in ("POSTGRES_PASSWORD", "NEBULA_PASSWORD", "MVP_API_KEY", "MVP_LOCAL_SESSION_SECRET", "LLM_CONFIG_ENCRYPTION_KEY"):
            secret = locals().get("values", {}).get(key)
            if secret:
                diagnostic = diagnostic.replace(secret, "[redacted]")
        with (RUNTIME / "logs/launcher.log").open("a", encoding="utf-8") as log:
            log.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n{diagnostic}")
        detail = str(error) if isinstance(error, RuntimeFailure) else f"{type(error).__name__}；请检查本地配置和运行日志。"
        say("\n启动器错误：" + detail)
        say("日志目录：" + str(RUNTIME / "logs"))
        if not arguments.no_pause and sys.stdin.isatty():
            input("按 Enter 关闭窗口...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
