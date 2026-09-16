@echo off
setlocal EnableExtensions
cd /d "%~dp0"
REM Keep Chinese diagnostics/comments readable when launched from the legacy
REM OEM code page.  The command is local to this build process and does not
REM change the user's persistent console setting.
chcp 65001 >nul

echo ==================================================
echo   PbootCMS 发文助手 W (WebUI) - EXE Builder
echo ==================================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python Launcher was not found.
  pause
  exit /b 1
)

if not exist ".build_env\Scripts\python.exe" (
  echo [1/5] Creating build environment...
  py -3 -m venv .build_env
  if errorlevel 1 goto :failed
) else (
  echo [1/5] Build environment found.
)

echo [2/5] Installing dependencies...
".build_env\Scripts\python.exe" -m pip install --upgrade pip
".build_env\Scripts\python.exe" -m pip install PyInstaller==6.21.0 -r requirements.txt
if errorlevel 1 goto :failed

if exist "test_smoke.py" (
  echo [3/5] Running developer regression tests...
  REM Developer workspaces keep test files locally.  Distribution source
  REM packages omit them, so release users can still build the application.
  REM Discovery includes every suite, for example test_product_optimizations.py.
  ".build_env\Scripts\python.exe" -m unittest discover -s . -p "test_*.py"
  if errorlevel 1 goto :failed
  if exist "audit_parity_repro.py" (
    ".build_env\Scripts\python.exe" -m unittest audit_parity_repro
    if errorlevel 1 goto :failed
  )
  if exist "test_ui_*.js" (
    where node >nul 2>nul
    if errorlevel 1 (
      echo [ERROR] Node.js is required to run UI parity tests.
      goto :failed
    )
    node --check webui\app.js
    if errorlevel 1 goto :failed
    for %%F in (test_ui_*.js) do (
      node "%%F"
      if errorlevel 1 goto :failed
    )
  )
) else (
  echo [3/5] Developer tests are not included; skipping regression tests.
)

for /f "delims=" %%I in ('.build_env\Scripts\python.exe build_support.py exe-basename') do set "APP_EXE_BASENAME=%%I"
if not defined APP_EXE_BASENAME goto :failed

".build_env\Scripts\python.exe" build_support.py metadata --output ".build_runtime\build_metadata.json"
if errorlevel 1 goto :failed

echo [4/5] Building EXE...
REM 注意：PyInstaller 的 Qt/中文路径问题——若本目录含中文导致构建失败，
REM 先 subst 一个 ASCII 盘符再构建（详见 R20 经验）。
if not exist "%APP_EXE_BASENAME%.spec" goto :failed
REM PyInstaller 6.21 may block while probing the bundled setuptools metadata
REM on this Windows build environment.  The application excludes setuptools,
REM so hide only those two build-time directories and always restore them.
set "SETUPTOOLS_DIR=.build_env\Lib\site-packages\setuptools"
set "SETUPTOOLS_INFO=.build_env\Lib\site-packages\setuptools-83.0.0.dist-info"
set "SETUPTOOLS_DIR_HIDDEN=.build_env\Lib\site-packages\setuptools.codex-disabled"
set "SETUPTOOLS_INFO_HIDDEN=.build_env\Lib\site-packages\setuptools-83.0.0.dist-info.codex-disabled"
if exist "%SETUPTOOLS_DIR%" ren "%SETUPTOOLS_DIR%" "setuptools.codex-disabled"
if errorlevel 1 goto :failed_restore_setuptools
if exist "%SETUPTOOLS_INFO%" ren "%SETUPTOOLS_INFO%" "setuptools-83.0.0.dist-info.codex-disabled"
if errorlevel 1 goto :failed_restore_setuptools
".build_env\Scripts\python.exe" -m PyInstaller --noconfirm --clean "%APP_EXE_BASENAME%.spec"
set "PYINSTALLER_RC=%ERRORLEVEL%"
if exist "%SETUPTOOLS_INFO_HIDDEN%" ren "%SETUPTOOLS_INFO_HIDDEN%" "setuptools-83.0.0.dist-info"
if exist "%SETUPTOOLS_DIR_HIDDEN%" ren "%SETUPTOOLS_DIR_HIDDEN%" "setuptools"
if not "%PYINSTALLER_RC%"=="0" goto :failed

if not exist "dist\%APP_EXE_BASENAME%.exe" goto :failed

echo [5/5] Applying optional Authenticode signature...
REM 可选签名环境变量（不得写入源码）：
REM   PBOOT_SIGN_CERT_SHA1       证书库中的 40 位指纹，或
REM   PBOOT_SIGN_PFX             PFX 文件路径
REM   PBOOT_SIGN_PFX_PASSWORD    PFX 密码（只从环境读取）
REM   PBOOT_SIGN_TIMESTAMP_URL   可选 RFC3161 时间戳地址
REM   PBOOT_SIGNTOOL             可选 signtool.exe 完整路径
REM   PBOOT_PUBLISHER_UPDATE_MANIFEST_URL  可选 HTTPS 发布清单地址
".build_env\Scripts\python.exe" build_support.py sign --exe "dist\%APP_EXE_BASENAME%.exe"
if errorlevel 1 goto :failed

copy /Y "dist\%APP_EXE_BASENAME%.exe" "%APP_EXE_BASENAME%.exe" >nul
if errorlevel 1 goto :failed

echo.
echo [DONE] %~dp0%APP_EXE_BASENAME%.exe
pause
exit /b 0

:failed
echo.
echo [FAILED] Build did not complete.
pause
exit /b 1

:failed_restore_setuptools
if exist "%SETUPTOOLS_INFO_HIDDEN%" ren "%SETUPTOOLS_INFO_HIDDEN%" "setuptools-83.0.0.dist-info"
if exist "%SETUPTOOLS_DIR_HIDDEN%" ren "%SETUPTOOLS_DIR_HIDDEN%" "setuptools"
goto :failed
