# Windows 打包说明

本项目是纯 Python 实现的远程桌面软件,包含三个独立组件:

- **host(被控端)**:常驻运行在被控制的 Windows 电脑上,入口 `host/main.py`(`host.main:main`)。
- **client(主控端)**:本地静态文件服务器启动器,双击后在本机启动网页并自动打开浏览器,入口 `client/main.py`(`client.main:main`)。
- **relay(中转服务器)**:部署在云端 Linux 服务器,**不需要**打包成 Windows exe。

普通用户不需要安装 Python,就能拿到可以直接双击运行的 `.exe`。本文档给出三条获取路径。

## 一、CI 自动构建(推荐,无需自己装环境)

仓库配置了 GitHub Actions workflow `.github/workflows/build-windows.yml`,每次代码 `push` 到仓库的**任意分支**时,都会自动在 `windows-latest` 虚拟机上用 PyInstaller 真实编译出 Windows exe。

获取方式:

1. 打开仓库的 **Actions** 标签页;
2. 找到最新一次 `build-windows` workflow 的运行记录(可以按分支/提交筛选);
3. 打开该次运行,在页面底部 **Artifacts** 区域下载名为 **`remotedesktop-windows`** 的压缩包;
4. 解压后即可看到:
   - `RemoteDesktop-Host.exe` —— 被控端
   - `RemoteDesktop-Client.exe` —— 主控端
   - `RemoteDesktop-Windows-Portable.zip` —— 绿色免安装整合包(内含上面两个 exe + 使用说明.txt)
   - `RemoteDesktopSetup.exe` —— 一键安装向导(如果 CI 里 Inno Setup 编译步骤成功的话;这一步失败不影响前面几个核心产物)

全程无需在自己电脑上安装 Python 或任何依赖。

也可以在 Actions 页面手动点击 **Run workflow**(`workflow_dispatch`)随时触发一次构建。

## 二、打 tag 获取正式发布版(带永久下载链接)

当仓库维护者执行:

```bash
git tag v1.0.0
git push origin v1.0.0
```

推送以 `v` 开头的 tag 会额外触发 GitHub Release 发布流程,自动创建一个 GitHub Release,并把以下文件作为 Release 附件永久挂在上面(通过 Releases 页面直接下载,无需去 Actions 页面翻找):

- **`RemoteDesktopSetup.exe`** —— 一键安装版:双击后走 Inno Setup 安装向导,安装到 `Program Files\RemoteDesktop`,自动创建开始菜单快捷方式("远程桌面-被控端" / "远程桌面-主控端"),可勾选创建桌面快捷方式。
- **`RemoteDesktop-Windows-Portable.zip`** —— 绿色免安装版:解压到任意目录即可直接使用,无需安装。
- `RemoteDesktop-Host.exe` / `RemoteDesktop-Client.exe` —— 两个独立 exe,供只需要单个组件的场景使用。

## 三、本地自行构建

在自己的 Windows 电脑(Windows 10/11,已安装 Python 3.10 及以上版本并加入 PATH)上:

```bat
git clone <本仓库地址>
cd RemoteDesktop
build\build_windows.bat
```

双击运行(或在命令行中执行)`build\build_windows.bat` 后,脚本会自动:

1. 创建并激活虚拟环境 `venv`;
2. `pip install -r requirements.txt`;
3. 安装 `pyinstaller` 与 `pystray`;
4. 用 PyInstaller 分别打包出 `RemoteDesktop-Host.exe`(保留控制台窗口)和 `RemoteDesktop-Client.exe`(无控制台窗口,内置 `client/static` 静态资源);
5. 在 `dist\` 目录下生成两个 exe、`使用说明.txt`,并打包成 `dist\RemoteDesktop-Windows-Portable.zip`。

几分钟后即可在 `dist\` 目录下拿到成品。如果还想生成一键安装包,额外安装 [Inno Setup 6](https://jrsoftware.org/isinfo.php) 后运行:

```bat
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss
```

会在 `build\Output\RemoteDesktopSetup.exe` 生成安装包。

## 打包相关文件一览

| 文件 | 作用 |
|---|---|
| `build/build_windows.bat` | 本地一键构建脚本(venv + 依赖 + PyInstaller + 打 zip) |
| `build/RemoteDesktop-Host.spec` | Host 的 PyInstaller spec(onefile, console=True,保留控制台窗口) |
| `build/RemoteDesktop-Client.spec` | Client 的 PyInstaller spec(onefile, console=False,内置 `client/static`) |
| `build/installer.iss` | Inno Setup 安装脚本,生成 `RemoteDesktopSetup.exe` |
| `.github/workflows/build-windows.yml` | GitHub Actions:在 windows-latest 上真实编译 exe,并在打 tag 时发布 Release |

## 给 `client/main.py` 作者的重要技术提醒

**`client/main.py` 被 PyInstaller 打包成 onefile exe 运行时,不能再用相对路径 `client/static` 定位网页静态资源目录**,因为 onefile 模式下所有资源会在运行时被解压到一个临时目录,PyInstaller 通过 `sys._MEIPASS` 暴露这个目录路径;而未打包(直接 `python client/main.py` 运行)时并没有这个属性。正确写法大致是:

```python
import sys, os

if getattr(sys, "frozen", False):
    # 被 PyInstaller 打包后运行(onefile 模式下 sys._MEIPASS 是解压后的临时目录)
    base_dir = sys._MEIPASS
else:
    # 直接用 python 解释器运行源码时
    base_dir = os.path.dirname(os.path.abspath(__file__))

static_dir = os.path.join(base_dir, "client", "static")
```

打包脚本(`build_windows.bat` / `RemoteDesktop-Client.spec` / CI workflow)这一侧已经通过 `--add-data "client/static;client/static"`(或 spec 文件里等价的 `datas` 配置)保证了 `client/static` 目录会被正确打进 exe;上面这段路径定位逻辑需要 `client/main.py` 自己实现,打包脚本无法替代。
