#!/usr/bin/env python3
"""ysp-live v9.8-TMXK 外挂式入口。

目标：不改 ysp-live-v9.8-TMXK.py 的任何内容，仍能加载 hongkong.py 的凤凰三台。

做法：
1. 加载 hongkong.py；
2. 加载 ysp-live-v9.8-TMXK.py；
3. 在运行时把凤凰频道合并进 ChannelDirectory；
4. 在运行时替换 Handler，让 /fs/status、/<slug>.flv 和 /all.m3u 走外挂逻辑；
5. 把核心的日志、HTTP、频道目录注入 hongkong；
6. 启动 hongkong 的 Token 线程，再调用核心 main()。

ysp-live-v9.8-TMXK.py 本身不会被修改，原有央视频/卫视逻辑全部保留。
"""

from __future__ import annotations

import importlib.util
import re
import sys
import threading
import urllib.parse
from pathlib import Path
from types import ModuleType

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# 优先使用 v9.8 原文件；Docker 里也可以用 ysp-live.py 软链
TARGET_CANDIDATES = ("ysp-live-v9.8-TMXK.py", "ysp-live.py")


def _load_module(module_name: str, filename: str) -> ModuleType:
    """按文件路径加载同目录 Python 模块，不执行目标文件的 __main__ 分支。"""
    path = BASE_DIR / filename
    if not path.is_file():
        raise SystemExit(f"找不到 {filename}，请确认它和 main.py 在同一目录：{BASE_DIR}")

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法为 {filename} 创建加载器")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except SystemExit:
        raise
    except Exception as exc:
        raise SystemExit(f"加载 {filename} 失败：{exc}") from exc
    return module


def _load_target() -> ModuleType:
    for filename in TARGET_CANDIDATES:
        if (BASE_DIR / filename).is_file():
            return _load_module("ysp_live", filename)
    raise SystemExit(f"找不到 {' 或 '.join(TARGET_CANDIDATES)}")


def _inject_hk_channels(ysp: ModuleType, hk: ModuleType, directory=None) -> None:
    """把 hongkong.channels() 合并进 v9.8 的频道目录。"""
    directory = directory if directory is not None else ysp.global_channel_dir
    existing = set(directory.channels_meta)
    for raw in hk.channels():
        if not isinstance(raw, dict):
            continue
        meta = dict(raw)
        slug = str(meta.get("slug") or "").strip().lower()
        name = str(meta.get("name") or "").strip()
        if not slug or not name or slug in existing:
            continue
        meta["slug"] = slug
        meta["name"] = name
        meta.setdefault("sid", "")
        meta.setdefault("pid", "")
        meta.setdefault("defn", "fhd")
        meta.setdefault("group", "其他")
        meta.setdefault("logo", f"{getattr(ysp, 'LOGO_BASE', '')}/{slug}.png")
        meta.setdefault("tvg_id", slug.upper())
        meta.setdefault("kind", "hls")
        meta.setdefault("ext", ".m3u8")
        directory.channels_meta[slug] = meta
        if slug not in directory.canonical_slugs:
            directory.canonical_slugs.append(slug)
        existing.add(slug)


def _patch_channel_directory(ysp: ModuleType, hk: ModuleType) -> None:
    """包装 load_defaults，保证核心以后重新装载频道时凤凰三台仍然在。"""
    original = ysp.ChannelDirectory.load_defaults
    if getattr(original, "_hongkong_patched", False):
        _inject_hk_channels(ysp, hk)
        return

    def load_defaults(self):
        original(self)
        _inject_hk_channels(ysp, hk, self)

    load_defaults._hongkong_patched = True
    ysp.ChannelDirectory.load_defaults = load_defaults
    ysp.global_channel_dir.load_defaults()


def _send_all_m3u(handler, ysp: ModuleType, head_only: bool) -> None:
    """按频道元数据里的 ext 输出地址，凤凰三台才能出 .flv。"""
    parts = urllib.parse.urlsplit(handler.path)
    host = handler.headers.get("Host", f"localhost:{ysp.server_port}")
    proto = handler.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip().lower() or "http"
    qs = urllib.parse.parse_qs(parts.query)
    target_group = qs.get("group", [None])[0]
    only_4k = qs.get("k4", ["0"])[0].lower() in ("1", "true", "yes")

    lines = [
        '#EXTM3U url-tvg="https://epg.zsdc.eu.org/t.xml.gz,https://epg.pw/xmltv/epg_CN.xml.gz" x-tvg-url="https://epg.zsdc.eu.org/t.xml.gz"',
        "#EXT-X-APTV-PREVIEW: FALSE",
        "#EXT-X-APTV-LATENCY: FALSE",
        "#EXT-X-APTV-LOGO: FALSE",
    ]
    for meta in ysp.global_channel_dir.unique_channels():
        slug = meta["slug"]
        name = meta["name"]
        group = meta["group"]
        if target_group and target_group not in group:
            continue
        if only_4k and slug not in ysp.TRUE_4K_CHANNELS:
            continue
        attrs = (
            f' tvg-id="{meta["tvg_id"]}" tvg-name="{name}" '
            f'tvg-logo="{meta["logo"]}" group-title="{group}"'
        )
        if slug in ysp.TIMESHIFT_SUPPORTED:
            attrs += ' catchup="append" catchup-days="7" catchup-source="?playseek=${(b)yyyyMMddHHmmss}-${(e)yyyyMMddHHmmss}"'
        lines.append(f"#EXTINF:-1{attrs},{name}")
        lines.append(f'{proto}://{host}/{slug}{meta.get("ext", ".m3u8")}')

    handler._send(
        200,
        "\n".join(lines) + "\n",
        "application/vnd.apple.mpegurl",
        head_only=head_only,
    )


def _make_handler(ysp: ModuleType, hk: ModuleType):
    """生成一个只做外挂路由的 Handler 子类，其余请求交回 v9.8 原实现。"""
    base_handler = ysp.Handler

    class ExtensionHandler(base_handler):
        def do_GET(self, head_only=False):
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path

            if path == "/fs/status" or re.match(r"^/([\w-]+)\.flv$", path):
                if hk.handle_request(self, path, parts, head_only):
                    return

            if path == "/all.m3u":
                _send_all_m3u(self, ysp, head_only)
                return

            return super().do_GET(head_only=head_only)

    return ExtensionHandler


def _install(ysp: ModuleType, hk: ModuleType) -> None:
    hk.configure(
        get_meta=ysp.global_channel_dir.get_meta,
        log_fn=ysp.log,
        urlopen=ysp.robust_urlopen,
        ua=ysp.UA,
        epg_get=None,
        epg_tz=None,
    )
    _patch_channel_directory(ysp, hk)
    ysp.Handler = _make_handler(ysp, hk)


def _start_hongkong(ysp: ModuleType, hk: ModuleType) -> None:
    """延迟 1 秒启动凤凰 Token 线程，让核心先把启动日志打完。"""
    try:
        hk.startup()
    except Exception as exc:
        ysp.log(f"[扩展] 凤凰模块启动失败: {exc}")


def main() -> int:
    hk = _load_module("hongkong", "hongkong.py")
    ysp = _load_target()
    _install(ysp, hk)
    if not any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        timer = threading.Timer(1.0, _start_hongkong, args=(ysp, hk))
        timer.daemon = True
        timer.start()
    result = ysp.main()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
