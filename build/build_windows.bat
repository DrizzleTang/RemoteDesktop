@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

rem =========================================================
rem   远程桌面助手 - Windows 一键打包脚本
rem   用途: 在装有 Python 3.10+ 的真实 Windows 电脑上运行,
rem        一条龙完成 虚拟环境 -> 安装依赖 -> PyInstaller 打包
rem        -> 生成绿色免安装 zip 压缩包
rem   用法: 双击本文件,或在命令行中运行
rem            build\build_windows.bat
rem =========================================================

rem 切换到仓库根目录(本脚本位于 build\ 目录下)
cd /d "%~dp0.."
echo 当前工作目录: %cd%
echo.

echo ========================================================
echo   远程桌面助手 - Windows 打包脚本
echo ========================================================
echo.

echo [1/7] 检查 Python 环境...
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python,请先安装 Python 3.10 或以上版本,并确保已加入 PATH。
    echo        下载地址: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo.

echo [2/7] 创建虚拟环境 venv ...
if exist "venv\Scripts\activate.bat" (
    echo 检测到已存在的虚拟环境,跳过创建。
) else (
    python -m venv venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败,请检查上方报错信息。
        pause
        exit /b 1
    )
)
echo.

echo [3/7] 激活虚拟环境 ...
call "venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [错误] 激活虚拟环境失败。
    pause
    exit /b 1
)
echo.

echo [4/7] 升级 pip 并安装项目依赖 (requirements.txt) ...
python -m pip install --upgrade pip
if errorlevel 1 (
    echo [错误] 升级 pip 失败。
    pause
    exit /b 1
)
pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 安装 requirements.txt 中的依赖失败,请检查网络或依赖名称是否正确。
    pause
    exit /b 1
)
echo.

echo [5/7] 安装打包所需的工具 (pyinstaller, pystray) ...
pip install pyinstaller pystray
if errorlevel 1 (
    echo [错误] 安装 pyinstaller / pystray 失败。
    pause
    exit /b 1
)
echo.

echo [6/7] 使用 PyInstaller 打包两个可执行文件 ...
echo   说明: PyInstaller 的临时构建目录使用 pybuild\,输出目录统一为 dist\,
echo         避免和本目录下的 build\ 打包脚本/配置文件互相冲突。
echo.

if exist "dist" (
    echo   清理旧的 dist\ 目录 ...
    rmdir /s /q "dist"
)
if exist "pybuild" (
    rmdir /s /q "pybuild"
)

echo   --------------------------------------------------
echo   正在打包 被控端(Host): RemoteDesktop-Host.exe
echo   ( 保留控制台窗口,因为启动时需要在控制台打印 "连接ID" 和 "连接密码" )
echo   --------------------------------------------------
if exist "build\RemoteDesktop-Host.spec" (
    pyinstaller --distpath "dist" --workpath "pybuild" --noconfirm "build\RemoteDesktop-Host.spec"
) else (
    echo   未找到 build\RemoteDesktop-Host.spec,改用命令行参数直接打包 ...
    pyinstaller --onefile --console --name "RemoteDesktop-Host" ^
        --distpath "dist" --workpath "pybuild" --noconfirm ^
        "host\main.py"
)
if errorlevel 1 (
    echo [错误] 打包 RemoteDesktop-Host.exe 失败,请检查上方 PyInstaller 报错信息。
    pause
    exit /b 1
)
echo.

echo   --------------------------------------------------
echo   正在打包 主控端(Client): RemoteDesktop-Client.exe
echo   ( 不显示控制台窗口,静态资源 client\static 已随 exe 一起打包 )
echo   --------------------------------------------------
if exist "build\RemoteDesktop-Client.spec" (
    pyinstaller --distpath "dist" --workpath "pybuild" --noconfirm "build\RemoteDesktop-Client.spec"
) else (
    echo   未找到 build\RemoteDesktop-Client.spec,改用命令行参数直接打包 ...
    pyinstaller --onefile --windowed --name "RemoteDesktop-Client" ^
        --distpath "dist" --workpath "pybuild" --noconfirm ^
        --add-data "client\static;client\static" ^
        "client\main.py"
)
if errorlevel 1 (
    echo [错误] 打包 RemoteDesktop-Client.exe 失败,请检查上方 PyInstaller 报错信息。
    pause
    exit /b 1
)
echo.

if not exist "dist\RemoteDesktop-Host.exe" (
    echo [错误] 未在 dist\ 目录下找到 RemoteDesktop-Host.exe,打包可能已失败。
    pause
    exit /b 1
)
if not exist "dist\RemoteDesktop-Client.exe" (
    echo [错误] 未在 dist\ 目录下找到 RemoteDesktop-Client.exe,打包可能已失败。
    pause
    exit /b 1
)

echo [7/7] 生成使用说明并打包为绿色免安装 zip ...

set "NOTE=dist\使用说明.txt"
echo 远程桌面助手 - 使用说明> "%NOTE%"
echo ========================================>> "%NOTE%"
echo.>> "%NOTE%"
echo 一、被控端(需要被远程控制的电脑)>> "%NOTE%"
echo    双击运行 RemoteDesktop-Host.exe>> "%NOTE%"
echo    程序启动后会弹出控制台窗口,并在其中显示"连接ID"和"连接密码",>> "%NOTE%"
echo    请将这两项信息告知使用主控端的一方。>> "%NOTE%"
echo    请保持该控制台窗口开启,关闭窗口将断开远程连接。>> "%NOTE%"
echo.>> "%NOTE%"
echo 二、主控端(用来控制对方电脑的一方)>> "%NOTE%"
echo    双击运行 RemoteDesktop-Client.exe>> "%NOTE%"
echo    程序会自动在本机启动一个网页服务,并用默认浏览器打开控制界面。>> "%NOTE%"
echo    在打开的网页中填入被控端提供的"连接ID"和"连接密码",>> "%NOTE%"
echo    点击连接即可开始远程控制。>> "%NOTE%"
echo.>> "%NOTE%"
echo 三、常见问题>> "%NOTE%"
echo    1. 双击后没有反应、或被拦截:请在 Windows Defender / 杀毒软件的>> "%NOTE%"
echo       提示中选择"仍要运行"，或将 exe 加入信任名单后重试。>> "%NOTE%"
echo    2. 无法连接:请确认被控端和主控端所在网络都能访问中转服务器,>> "%NOTE%"
echo       并确认"连接ID"和"连接密码"输入无误。>> "%NOTE%"
echo    3. 本程序为绿色版,无需安装,可直接复制到任意目录使用;>> "%NOTE%"
echo       卸载时直接删除对应的 exe 文件即可。>> "%NOTE%"

if not exist "%NOTE%" (
    echo [错误] 生成使用说明.txt 失败。
    pause
    exit /b 1
)

set "STAGE=dist\RemoteDesktop-Windows-Portable"
if exist "%STAGE%" rmdir /s /q "%STAGE%"
mkdir "%STAGE%"
copy /y "dist\RemoteDesktop-Host.exe" "%STAGE%\" >nul
copy /y "dist\RemoteDesktop-Client.exe" "%STAGE%\" >nul
copy /y "%NOTE%" "%STAGE%\" >nul

if exist "dist\RemoteDesktop-Windows-Portable.zip" (
    del /f /q "dist\RemoteDesktop-Windows-Portable.zip"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Compress-Archive -Path '%STAGE%\*' -DestinationPath 'dist\RemoteDesktop-Windows-Portable.zip' -Force"
if errorlevel 1 (
    echo [错误] 使用 PowerShell 生成 zip 压缩包失败。
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   打包完成!
echo.
echo   可执行文件:
echo     dist\RemoteDesktop-Host.exe        (被控端,双击后在控制台查看连接ID/密码)
echo     dist\RemoteDesktop-Client.exe      (主控端,双击后自动打开控制网页)
echo.
echo   绿色免安装压缩包:
echo     dist\RemoteDesktop-Windows-Portable.zip
echo ========================================================
echo.
pause
exit /b 0
