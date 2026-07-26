# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec 文件:打包 被控端(Host)
#
# 用法(必须在仓库根目录下执行,build_windows.bat 已经保证了这一点):
#     pyinstaller --distpath dist --workpath pybuild --noconfirm build/RemoteDesktop-Host.spec
#
# 说明:
# - Host 是常驻在被控制电脑上的程序,需要保留控制台窗口(console=True),
#   因为程序启动时要在控制台打印本机的"连接ID"和"连接密码",供用户
#   查看并告知主控端使用者;不要改成 --noconsole/--windowed。
# - onefile 模式:所有依赖打进单一 exe,双击即可运行,无需安装 Python。
# - hiddenimports 里列出了 pynput / mss / pyperclip 在 Windows 下常见的
#   后端子模块名。如果实际模块路径与此处不完全一致也没关系,PyInstaller
#   对不存在的 hiddenimports 只会忽略,不会导致打包失败。
#
# 注: 使用 SPECPATH(PyInstaller 自动注入的"本 spec 文件所在目录"变量)
#     推算仓库根目录,这样无论从仓库根目录还是从 build\ 目录下执行
#     pyinstaller 命令,路径都能正确解析。

import os
REPO_ROOT = os.path.dirname(os.path.abspath(SPECPATH))

a = Analysis(
    [os.path.join(REPO_ROOT, 'host', 'main.py')],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=[],
    hiddenimports=[
        # pynput 在 Windows 下的键盘/鼠标后端
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'pynput.keyboard._base',
        'pynput.mouse._base',
        # 剪贴板同步
        'pyperclip',
        # 屏幕采集
        'mss',
        'mss.windows',
        # WebSocket 服务端
        'websockets',
        'websockets.server',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除运行时完全用不到的重型模块,显著减小 exe 体积:
    #   numpy 自带的 distutils/f2py/测试套件约 5MB;tkinter/matplotlib 等本项目
    #   从未使用,但 PyInstaller 的依赖分析有时会把它们连带进来。
    excludes=[
        'numpy.distutils', 'numpy.f2py', 'numpy.testing', 'numpy.tests',
        'numpy.random.tests', 'numpy.doc',
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
    name='RemoteDesktop-Host',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 保留控制台窗口:显示"连接ID"/"连接密码"
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
