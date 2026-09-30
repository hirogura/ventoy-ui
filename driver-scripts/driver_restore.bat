@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem --- 管理者権限チェック(なければ昇格して再実行) ---
fltmc >nul 2>&1 || (
    echo 管理者権限で再起動します...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

set "DRV=%~dp0drivers"
if not exist "%DRV%" (
    echo [エラー] drivers フォルダが見つかりません: %DRV%
    echo.
    pause
    exit /b 1
)

echo ドライバーの復元を開始します...
echo 復元元: %DRV%
echo.

pnputil /add-driver "%DRV%\*.inf" /subdirs /install
echo.
echo 終了コード: %errorlevel%
echo 復元処理が完了しました。
echo.
pause
