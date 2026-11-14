# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir）。

选 onedir 而不是 onefile：onefile 每次启动都要把整个目录解压到临时路径，
启动慢且杀软容易误报；onedir 目录结构清晰，也方便把 data 放在 exe 旁边。

llama.cpp 与 CUDA 运行时不打包——合计 1.3GB，且只有真正跑推理才需要，
交给首次运行时下载。
"""

from PyInstaller.utils.hooks import collect_submodules

hidden = [
    # uvicorn 是动态导入，PyInstaller 静态分析抓不到
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 后端注册靠 import 触发，入口在包 __init__
    "aether.backends.llama_cpp",
    # websockets 15 的内部实现路径
    "websockets.legacy",
    "websockets.legacy.client",
    "websockets.asyncio.client",
]

# 关键：uvicorn 用字符串 "aether.api.app:app" 导入应用，
# 静态分析看不到这条路径，必须显式收集全部子模块
hidden += collect_submodules("aether")

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[("aether/gui", "aether/gui")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # 这些是开发期或 e2e 才用到的重依赖，打进去只会让包变大
    excludes=["tkinter", "matplotlib", "torch", "numpy", "pandas", "PIL", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AetherNode",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # 保留控制台：--no-window 模式下需要看日志
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AetherNode",
)
