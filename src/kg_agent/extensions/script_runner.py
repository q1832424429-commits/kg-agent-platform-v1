"""在无网络、无业务凭证的独立容器中执行用户上传的 Skill 脚本。

本模块只使用 Python 标准库。主应用通过共享作业卷交付请求，Runner 不连接数据库、
MinIO 或模型 API；每个脚本进程降权并限制 CPU、内存、输出文件和运行时间。
"""

from __future__ import annotations

import ast
import base64
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_RESOURCE_BYTES = 2 * 1024 * 1024
_MAX_RESULT_BYTES = 2 * 1024 * 1024
_MAX_REQUEST_BYTES = 12 * 1024 * 1024


def _safe_path(value: str) -> PurePosixPath:
    """校验资源位于作业目录内，禁止绝对路径和目录穿越。"""

    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.name in {"", "."}:
        raise ValueError("脚本资源路径非法")
    return path


def _limit_child(uid: int, timeout_seconds: int, sandbox: Path) -> None:
    """设置系统限制、降权，再进入仅属于本作业 UID 的沙箱目录。"""

    resource.setrlimit(resource.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
    # pandas/openpyxl 等批准依赖加载时需要高于纯标准库脚本的地址空间；容器本身仍有
    # 512 MiB 硬限制，单个子进程最多使用 384 MiB。
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_RESULT_BYTES, _MAX_RESULT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    # subprocess 的 cwd 切换早于 preexec_fn；在最小 capability 容器中，Runner
    # root 无权进入属于目标 UID 的 0700 目录，因此必须降权后在这里切换。
    os.chdir(sandbox)


def _has_run_function(entry_source: str, entrypoint: str) -> bool:
    """在权限移交前识别函数式入口，避免 Runner 再读取私有沙箱文件。"""

    tree = ast.parse(entry_source, filename=entrypoint)
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"
        for node in tree.body
    )


def execute_job(job: Path) -> dict[str, Any]:
    """读取一份已完整写入的请求，隔离运行入口脚本并返回结构化响应。"""

    request_path = job / "request.json"
    if request_path.stat().st_size > _MAX_REQUEST_BYTES:
        raise ValueError("脚本作业请求过大")
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    resources = payload["resources"]
    entrypoint = str(_safe_path(payload["entrypoint"]))
    if not isinstance(resources, list) or len(resources) > 30:
        raise ValueError("脚本资源数量非法")
    uid = 20000 + (int(job.name.replace("-", "")[:8], 16) % 40000)
    # 作业目录只允许非特权子进程穿越，不允许列目录或修改控制文件。
    job.chmod(0o711)
    sandbox = job / "sandbox"
    sandbox.mkdir(mode=0o700)
    for item in resources:
        relative = _safe_path(item["path"])
        content = base64.b64decode(item["content_base64"], validate=True)
        if len(content) > _MAX_RESOURCE_BYTES:
            raise ValueError("单个脚本资源不能超过 2 MiB")
        destination = sandbox.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    if not (sandbox / entrypoint).is_file():
        raise ValueError("脚本入口文件未上传")
    # Runner 已移除 DAC_OVERRIDE。沙箱转交给随机降权 UID 并收紧为 0700/0600 后，
    # 主进程不能再读取入口文件，因此必须在权限移交之前完成静态入口识别。
    entry_source = (sandbox / entrypoint).read_text(encoding="utf-8")
    has_run_function = _has_run_function(entry_source, entrypoint)
    record_adapter = payload.get("input", {}).get("parameters", {}).get("entity_record_adapter")
    if record_adapter is not None and not isinstance(record_adapter, dict):
        raise ValueError("实体逐项适配配置非法")
    # 从叶子到根目录移交所有权；若先把 0700 根目录交给目标 UID，已经移除
    # DAC_OVERRIDE 的 Runner 自身就无法继续访问其子项。
    entries = sorted(sandbox.rglob("*"), key=lambda item: len(item.parts), reverse=True)
    entries.append(sandbox)
    for entry in entries:
        if entry.is_dir():
            entry.chmod(0o700)
            os.chown(entry, uid, uid)
        else:
            entry.chmod(0o600)
            os.chown(entry, uid, uid)
    timeout_seconds = min(max(int(payload.get("timeout_seconds", 30)), 1), 300)
    started = time.monotonic()
    input_bytes = json.dumps(payload["input"], ensure_ascii=False).encode("utf-8")
    output_path, error_path = job / "stdout", job / "stderr"
    # 已确认的实体逐项适配与 run(inputs, parameters) 共用固定平台入口；
    # 没有适配配置且没有 run 的历史脚本继续按原标准输入/输出协议执行。
    command = (
        [sys.executable, "-B", "/runner/script_entry.py", entrypoint]
        if has_run_function or record_adapter is not None
        else [sys.executable, "-B", entrypoint]
    )
    with output_path.open("wb") as output, error_path.open("wb") as error:
        try:
            completed = subprocess.run(
                # Runner 镜像只安装平台批准依赖，且容器无网络、无业务凭证。这里允许
                # 读取该固定 site-packages，同时保留脚本目录以导入同包辅助模块。
                command,
                input=input_bytes,
                stdout=output,
                stderr=error,
                timeout=timeout_seconds,
                check=False,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    # subprocess 显式重建环境后不会继承镜像 ENV；这里必须恢复平台
                    # 批准依赖目录，否则降权脚本无法导入 pandas 等预装依赖。
                    "PYTHONPATH": "/usr/local/lib/python3.12/site-packages",
                    "PYTHONUNBUFFERED": "1",
                    # 数值库不得按宿主 CPU 数量创建大量线程；脚本子进程仍受单任务资源上限。
                    "OPENBLAS_NUM_THREADS": "1",
                    "OMP_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                },
                preexec_fn=lambda: _limit_child(uid, timeout_seconds, sandbox),
            )
        except subprocess.TimeoutExpired:
            return {"error_code": "SCRIPT_TIMEOUT", "error_message": "脚本执行超时"}
    elapsed_ms = round((time.monotonic() - started) * 1000, 3)
    if completed.returncode:
        error_text = error_path.read_bytes()[:2000].decode("utf-8", errors="replace")
        return {
            "error_code": "SCRIPT_EXIT_ERROR",
            "error_message": f"脚本退出码 {completed.returncode}: {error_text}",
            "duration_ms": elapsed_ms,
        }
    if output_path.stat().st_size > _MAX_RESULT_BYTES:
        raise ValueError("脚本输出超过 2 MiB")
    result = json.loads(output_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("脚本标准输出必须是 JSON 对象")
    return {"result": result, "duration_ms": elapsed_ms}


def main() -> None:
    """轮询共享卷；每个作业只处理一次，响应由客户端确认后清理。"""

    root = Path(os.environ.get("SCRIPT_RUNNER_JOBS_DIR", "/jobs"))
    root.mkdir(parents=True, exist_ok=True)
    while True:
        for job in root.iterdir():
            if not job.is_dir():
                continue
            if (job / "ack").exists() or (
                (job / "response.json").exists()
                and time.time() - (job / "response.json").stat().st_mtime > 600
            ):
                shutil.rmtree(job, ignore_errors=True)
                continue
            request_path = job / "request.json"
            if not request_path.is_file() or (job / "response.json").exists():
                continue
            try:
                response = execute_job(job)
            except Exception as exc:
                response = {"error_code": "SCRIPT_RUNNER_ERROR", "error_message": str(exc)[:2000]}
            temporary = job / "response.tmp"
            temporary.write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
            temporary.replace(job / "response.json")
        time.sleep(0.2)


if __name__ == "__main__":
    main()
