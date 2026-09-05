# -*- coding: utf-8 -*-
"""用正式客户端账号数据启动当前工作树源码。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterable, Iterator, Mapping


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_core.source_live_runtime import (  # noqa: E402
    SourceLiveRuntimeError,
    build_source_live_environment,
    source_live_session,
)


@dataclass(frozen=True)
class SourceLivePaths:
    production_data_dir: Path
    backup_root: Path
    marker_path: Path


@dataclass(frozen=True)
class PreparedSourceLaunch:
    command: list[str]
    environment: dict[str, str]
    backup_dir: Path


def default_source_live_paths(home: Path | str = Path.home()) -> SourceLivePaths:
    current_home = Path(home).expanduser().absolute()
    application_support = current_home / "Library" / "Application Support"
    production = application_support / "一键发"
    return SourceLivePaths(
        production_data_dir=production,
        backup_root=application_support / "一键发开发版" / "backups",
        marker_path=production / "source-live-session.json",
    )


def _process_rows() -> list[str]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.splitlines()


@contextmanager
def prepare_source_launch(
    project_root: Path | str,
    *,
    production_data_dir: Path | str,
    backup_root: Path | str,
    marker_path: Path | str,
    process_rows: Iterable[str],
    backup_name: str,
    current_pid: int,
    page: str,
    base_environment: Mapping[str, str],
) -> Iterator[PreparedSourceLaunch]:
    project = Path(project_root).expanduser().absolute()
    python = project / ".venv" / "bin" / "python"
    entrypoint = project / "desktop_native_app.py"
    if not python.is_file() or not entrypoint.is_file():
        raise SourceLiveRuntimeError("当前工作树缺少源码虚拟环境或桌面入口")
    with source_live_session(
        production_data_dir,
        backup_root,
        process_rows=process_rows,
        marker_path=marker_path,
        backup_name=backup_name,
        current_pid=current_pid,
    ) as session:
        yield PreparedSourceLaunch(
            command=[
                str(python),
                "-u",
                str(entrypoint),
                "--page",
                str(page),
            ],
            environment=build_source_live_environment(
                base_environment, session.production_data_dir
            ),
            backup_dir=session.backup_dir,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--page",
        choices=(
            "workspace",
            "accounts",
            "media",
            "montage",
            "publish",
            "douyin_graphic_matrix",
            "commerce",
            "tasks",
            "data",
        ),
        default="commerce",
    )
    args = parser.parse_args()
    paths = default_source_live_paths()
    backup_name = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    try:
        with prepare_source_launch(
            ROOT,
            production_data_dir=paths.production_data_dir,
            backup_root=paths.backup_root,
            marker_path=paths.marker_path,
            process_rows=_process_rows(),
            backup_name=backup_name,
            current_pid=os.getpid(),
            page=args.page,
            base_environment=os.environ,
        ) as prepared:
            print(f"源码联调数据：{paths.production_data_dir}", flush=True)
            print(f"启动前备份：{prepared.backup_dir}", flush=True)
            completed = subprocess.run(
                prepared.command,
                cwd=ROOT,
                env=prepared.environment,
                check=False,
            )
            return int(completed.returncode)
    except (OSError, subprocess.SubprocessError, SourceLiveRuntimeError) as exc:
        print(f"一键发源码联调启动失败：{exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
