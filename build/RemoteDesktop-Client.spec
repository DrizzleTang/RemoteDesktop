# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec 文件:打包 主控端(Client)
#
# 用法(必须在仓库根目录下执行,build_windows.bat 已经保证了这一点):
#     pyinstaller --distpath dist --workpath pybuild --noconfirm build/RemoteDesktop-Client.spec
#
# 说明:
# - Client 只是"本地静态文件服务器启动器":启动一个 HTTP 服务器托管
#   client/static/ 下的网页,并自动用默认浏览器打开,因此不需要控制台
#   窗口(console=False,等价于 --noconsole/--windowed)。
# - datas 把整个 client/static 目录打进 onefile exe。
#
# !! 重要提醒(给 client/main.py 的作者)!!
#   onefile 模式下,exe 运行时会把打包内容解压到一个临时目录,PyInstaller
#   通过 sys._MEIPASS 暴露这个临时目录的路径。也就是说,直接用相对路径
#   "client/static" 在打包后的 exe 里是找不到的,必须在代码里做类似:
#
#       import sys, os
#       if getattr(sys, "frozen", False):
#           base_dir = sys._MEIPASS
#       else:
#           base_dir = os.path.dirname(os.path.abspath(__file__))
#       static_dir = os.path.join(base_dir, "client", "static")
#
#   这是应用代码(client/main.py)的职责,本 spec 文件只负责保证
#   client/static 目录被正确打包进 exe。
#
# 注: 使用 SPECPATH(PyInstaller 自动注入的"本 spec 文件所在目录"变量)
#     推算仓库根目录,这样无论从仓库根目录还是从 build\ 目录下执行
#     pyinstaller 命令,路径都能正确解析。

import os
REPO_ROOT = os.path.dirname(os.path.abspath(SPECPATH))

a = Analysis(
    [os.path.join(REPO_ROOT, 'client', 'main.py')],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=[
        (os.path.join(REPO_ROOT, 'client', 'static'), os.path.join('client', 'static')),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除运行时完全用不到的重型模块,显著减小 exe 体积:
    #   numpy 自带的 distutils/f2py/测试套件约 5MB;tkinter/matplotlib 等本项目
    #   从未使用,但 PyInstaller 的依赖分析有时会把它们连带进来。
    # 主控端只是一个静态文件服务器 + 打开浏览器,全部用标准库实现,
    # 完全不需要 numpy/Pillow/cryptography/mss/pynput 这些被控端专用的重型
    # 依赖(真正的客户端逻辑跑在浏览器里)。全部排除可以让这个 exe 小很多。
    excludes=[
        'numpy', 'PIL', 'cryptography', 'mss', 'pynput', 'pyperclip', 'pystray',
        'tkinter', 'matplotlib', 'scipy', 'pandas', 'pytest', '_pytest',
        'setuptools', 'pip', 'wheel', 'doctest', 'pdb', 'unittest',
        'PIL.ImageQt', 'PIL.ImageTk',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RemoteDesktop-Client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无控制台窗口:只启动本地服务器 + 打开浏览器
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
