#!/usr/bin/env python
"""零文档语料库建夹验收（回归：2026-09-11 用户报"建不了新文件夹"）。

为什么单独有这个脚本：chrome_corpus.py 的流程是"先上传文档、再建文件夹"，
永远走不到**空语料库**这条路径——而 bug 恰恰只在空库时出现。kb-tree 的
renderTree 曾经拿 `docsCache.length` 当空态判据，一篇文档都没有时提前
return，把后面的文件夹渲染整段跳过：建夹请求 201 成功、行也进了库，界面
上却始终只有"还没有文档"占位。用户以为没建成便反复新建（实测库里躺着
连建的同名文件夹 5 个）。

用法：
  python tools/chrome_empty_folder.py --url http://127.0.0.1:8787/documents
退出码：0 = 有文件夹就渲染出了文件夹；1 = 被空态占位挡掉。
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.chrome_probe import CDP, log, wait_json_list  # noqa: E402

CHROME_FALLBACKS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_chrome() -> str:
    cands = CHROME_FALLBACKS + [
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    raise SystemExit("找不到 Chrome/Edge")


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run(args) -> int:
    base = args.url.rsplit("/documents", 1)[0]

    # 服务端真相：库里到底有几个文件夹、几篇文档
    with urllib.request.urlopen(f"{base}/api/kb-folders", timeout=5) as r:
        server_folders = json.loads(r.read().decode("utf-8"))["folders"]
    with urllib.request.urlopen(f"{base}/api/documents", timeout=5) as r:
        server_docs = json.loads(r.read().decode("utf-8"))["documents"]

    cdp_port = free_port()
    profile = tempfile.mkdtemp(prefix="empty-folder-chrome-")
    chrome = subprocess.Popen(
        [
            find_chrome(),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={cdp_port}",
            f"--user-data-dir={profile}",
            "--window-size=1400,900",
            args.url,
        ]
    )
    try:
        target = wait_json_list(cdp_port)
        async with CDP(target["webSocketDebuggerUrl"]) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Log.enable")
            deadline = time.time() + 20
            while time.time() < deadline:
                if await cdp.evaluate(
                    "document.readyState === 'complete' && !!document.querySelector('#kb-tree')"
                ):
                    break
                await asyncio.sleep(0.2)
            await asyncio.sleep(1.5)  # 首载 refreshCorpusTree 网络往返

            dom = await cdp.evaluate("""(() => ({
              folders: document.querySelectorAll('#kb-tree .t-folder').length,
              docs: document.querySelectorAll('#kb-tree .doc-item').length,
              empty: document.querySelector('#kb-tree .empty')?.textContent || '',
            }))()""")
    finally:
        chrome.terminate()

    print(f"服务端：{len(server_folders)} 个文件夹 / {len(server_docs)} 篇文档")
    print(f"页面渲染：{dom['folders']} 个文件夹行 / {dom['docs']} 个文档行 / 占位={dom['empty']!r}")

    if server_folders and not dom["folders"]:
        log("FAIL：库里有文件夹，页面上一个都没渲染出来（被空态占位挡掉）")
        return 1
    if dom["empty"] and server_folders:
        log("FAIL：有文件夹却仍显示空库占位")
        return 1
    print("PASS：文件夹渲染正常")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8787/documents")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
