"""应用内更新的四个端点（ADR-0022）。

  GET  /api/update/check             查有没有新版（前端启动时静默调用）
  POST /api/update/download          启动后台下载（202 + 轮询）
  GET  /api/update/download/status   下载进度（前端 ~0.5s 轮询）
  POST /api/update/install           启动安装器（此后本进程会被安装器结束）

**响应里永远不含下载地址**：前端只会说"下载最新版"，URL 由服务端从
GitHub API 响应里挑（release.py）。这样客户端无从指定任何地址，
SSRF 面收窄到"服务端自己挑出来的那一条链"（install.py 的主机白名单）。

检查失败对前端是静默的（离线不该被打扰），但错误仍以 502/400 的形状
到达——前端自己决定吞不吞；日志里留一行。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from mikasa import __version__
from mikasa.config.settings import Settings
from mikasa.update import is_newer
from mikasa.update.install import launch_installer, run_download
from mikasa.update.release import release_page_url
from mikasa.utils.logging import get_logger
from mikasa.web.deps import get_services, get_settings
from mikasa.web.services import AppServices

router = APIRouter(tags=["update"])
logger = get_logger("web.routers.update")


@router.get("/api/update/check")
def check_update(
    force: bool = False,
    services: AppServices = Depends(get_services),
) -> dict:
    """最新版本信息（TTL 内走缓存；force=true 强制刷新）。"""
    release = services.updates.cached(force=force)
    asset = release.setup_asset()
    available = is_newer(release.version, __version__)
    if not available:
        logger.info("检查更新：当前 %s 已是最新（远端 %s）", __version__, release.version)
    return {
        "current": __version__,
        "latest": release.version,
        "update_available": available,
        "release_url": release.html_url or release_page_url(release.tag),
        "notes": release.notes,
        "published_at": release.published_at,
        # 只给名字与大小：有没有可一键安装的包，前端据此决定按钮文案
        "asset": {"name": asset.name, "size": asset.size} if asset is not None else None,
        "install_supported": os.name == "nt",
    }


def _updates_dir(settings: Settings):
    """下载落点：数据目录下的 updates/（不放 %TEMP%——清理工具会顺手清掉）。"""
    return settings.data_dir / "updates"


@router.post("/api/update/download", status_code=202)
def start_download(
    background: BackgroundTasks,
    settings: Settings = Depends(get_settings),
    services: AppServices = Depends(get_services),
) -> dict:
    """占槽并启动后台下载。同步预检：必须有更新、必须有安装包资产。"""
    release = services.updates.cached()
    if not is_newer(release.version, __version__):
        raise HTTPException(status_code=400, detail="当前已是最新版本，无需下载")
    asset = release.setup_asset()
    if asset is None:
        raise HTTPException(
            status_code=400,
            detail="这个版本里没有安装包（Mikasa-Setup-*-win64.exe），请到发布页手动下载",
        )
    if not services.update_jobs.start(release.version, asset.name):
        raise HTTPException(status_code=409, detail="更新下载已经开始了")
    background.add_task(run_download, services.update_jobs, release, _updates_dir(settings))
    logger.info("开始下载更新：%s", asset.name)
    return {"status": "running", "version": release.version, "asset_name": asset.name}


@router.get("/api/update/download/status")
def download_status(services: AppServices = Depends(get_services)) -> dict:
    """下载进度快照（无任务时 status=idle）。"""
    snapshot = services.update_jobs.snapshot()
    return snapshot if snapshot is not None else {"status": "idle"}


@router.post("/api/update/install")
def install_update(
    settings: Settings = Depends(get_settings),
    services: AppServices = Depends(get_services),
) -> dict:
    """启动安装器。成功返回后本进程很快会被安装器结束（它先 taskkill）。"""
    path = services.update_jobs.result_path()
    if path is None:
        raise HTTPException(status_code=400, detail="还没有下载完成的安装包")
    launch_installer(path, _updates_dir(settings))
    logger.info("安装器已启动：%s", path.name)
    return {"status": "launched"}
