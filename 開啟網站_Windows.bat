@echo off
:: 遊戲整形 Web UI：Windows 啟動檔
:: 注意：本檔必須以 CRLF 換行、UTF-8 無 BOM 儲存，否則 cmd 會讀錯行
:: 注意：中文訊息不要放進 ( ) 區塊內，也不要使用全形驚嘆號，UTF-8 下 cmd 會讀錯行
chcp 65001 >nul
title 遊戲整形 Web UI

:: 偵測 Python：依序實際執行一次，避開 Microsoft Store 的空殼 python / python3，並要求 3.7 以上
set "py_cmd="
for %%P in (py python python3) do (
    if not defined py_cmd (
        %%P -c "import sys; sys.exit(sys.version_info[:2] ^< (3, 7))" >nul 2>nul && set "py_cmd=%%P"
    )
)
if not defined py_cmd goto no_python

:: 以本檔所在位置找 app，從任何位置雙擊都能執行；伺服器會自動選擇可用連接埠並開啟瀏覽器
%py_cmd% "%~dp0app\server.py"
if errorlevel 1 goto app_failed
exit /b 0

:no_python
echo 錯誤: 這台電腦找不到 Python 3.7 以上的版本，網站無法啟動。
echo.
echo 請到下列網址下載並安裝 Python：
echo   https://www.python.org/downloads/
echo 安裝時第一個畫面要勾選「Add python.exe to PATH」，裝好後再雙擊本檔一次。
echo 詳細操作方式請看同一個資料夾裡的「使用方法.txt」。
goto wait_exit

:app_failed
echo.
echo 網站已停止。如果上方有錯誤訊息，請截圖提供給維護者。

:wait_exit
echo.
echo 按任意鍵關閉此視窗...
pause >nul
exit /b 1
