#!/bin/zsh
# 遊戲整形 Web UI：Mac 啟動檔
# 注意：本檔必須是 LF 換行，而且要有執行權限

wait_exit() {
    echo
    echo "按任意鍵關閉此視窗..."
    read -k 1
    exit 1
}

# 以本檔所在位置找 app，從任何位置雙擊都能執行
cd "$(dirname "$0")" || {
    echo "錯誤: 無法進入啟動檔所在的資料夾，請確認共用磁碟連線正常。"
    wait_exit
}

# 偵測 Python：依序實際執行一次，並要求 3.7 以上。
# /usr/bin/python3 是 macOS 內建的空殼：沒裝「開發者工具」時一執行就會跳出安裝視窗，
# 所以只有在開發者工具已安裝時才試它。
py_cmd=""
for candidate in \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    $(whence -p python3) \
    $(whence -p python)
do
    [[ -x "$candidate" ]] || continue
    if [[ "$candidate" == /usr/bin/* ]] && ! xcode-select -p &>/dev/null; then
        continue
    fi
    if "$candidate" -c 'import sys; sys.exit(sys.version_info[:2] < (3, 7))' &>/dev/null; then
        py_cmd="$candidate"
        break
    fi
done

if [[ -z "$py_cmd" ]]; then
    echo "\033[0;31m錯誤: 這台電腦找不到 Python 3.7 以上的版本，網站無法啟動。\033[0m"
    echo
    echo "請到下列網址下載並安裝 Python（選 macOS 的安裝檔）："
    echo "  https://www.python.org/downloads/macos/"
    echo "裝好後再雙擊本檔一次。詳細操作方式請看同一個資料夾裡的「使用方法.txt」。"
    wait_exit
fi

# 伺服器會自動選擇可用連接埠並開啟瀏覽器；關閉此終端機視窗即停止網站。
"$py_cmd" app/server.py || {
    echo
    echo "網站已停止。如果上方有錯誤訊息，請截圖提供給維護者。"
    wait_exit
}
