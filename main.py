#!/usr/bin/env python3
"""ysp-live 统一入口。

启动顺序：
1. 先把同目录的 hongkong.py 加载并注册为 ``hongkong``；
2. 再加载 ysp-live-v9.5.py；
3. 把 hongkong 注册为核心扩展，然后调用 ysp-live 的 main()。

ysp-live-v9.5.py 本身不包含任何凤凰/香港三台逻辑，只提供通用扩展接口；
原有命令行参数、端口、环境变量全部保持不变。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

BASE_DIR = Path(__file__).resolve().parent
# 保证从任意工作目录启动时，都能 import 同目录的 hongkong.py
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_module(module_name: str, filename: str) -> ModuleType:
    """按文件路径加载同目录下的 Python 模块，并注册到 sys.modules。"""
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


def load_modules() -> tuple[ModuleType, ModuleType]:
    """加载凤凰模块和 ysp-live 主模块，并完成扩展注册。"""
    hk = _load_module("hongkong", "hongkong.py")
    ysp = _load_module("ysp_live_v95", "ysp-live-v9.5.py")
    if hk not in ysp.EXTENSIONS:
        ysp.register_extension(hk)
    return hk, ysp


def main() -> int:
    """启动 ysp-live；参数原样交给 ysp-live-v9.5.py 的 argparse。"""
    _hk, ysp = load_modules()
    result = ysp.main()
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
