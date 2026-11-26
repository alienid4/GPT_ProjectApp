# 工作管理平台 — 開發中來源

版本：0.1.0。此儲存庫是去除環境資訊的開發來源快照，**不是已驗收的正式版本，也不是完整離線一鍵安裝包**。

提供個人工作看板、多層專案與 WBS、甘特圖、成果確認、附件版本、討論、權限、管理及匯出功能。採 Python 3.11+、Flask、SQLAlchemy 與 Alembic。

## 升級／安裝：放 /tmp → 執行 up-patch（2.002.0 起，一條路）

每一版只出一個包：`patch_projectapp_<版本>_<yyyymmddHHMM>.tar.gz`（內含完整安裝包約 85 MB，超過 50 MB 所以放在 Releases 頁面該版本的附件；同名 `.sha256` 可先核對）。

**下載 Releases 裡的 `patch_projectapp_*.tar.gz` → 放 /tmp → 執行 up-patch。** 目前最新：[full-2.002.0](https://github.com/alienid4/GPT_ProjectApp/releases/tag/full-2.002.0)（`patch_projectapp_2.002.0_202611261902.tar.gz`）。不用再分修補包或完整包，也不用帶參數：

```bash
# 1. 把 patch_projectapp_<版本>_<時間>.tar.gz 放進 /tmp
# 2. 執行（跟 WEBIT 一樣）
sh /tmp/up-patch.ksh
# 3. 看最後一行：
#    OK ProjectApp 2.001.0 → 2.002.0
#    FAIL 已自動退回 2.001.0，原因：…，log：/var/log/projectapp/patch_<時間>.log
```

包裡的 `patch.sh` 會自己判斷這台是：**全新機**（已先跑過設定精靈）、**v1.000.0**（`/opt/it-workboard`，原地接手）、**v1.001.x／2.x**（升級）或**已經是同一版**（只核對，回報「已是 X」）。

- **不是 ProjectApp 的主機立刻停、什麼都不改**（例如 WEBIT 主機上 /tmp 最新一包剛好是這個）。
- 先做 sha256 檢查；升級前自動備份資料庫；提交前任何一步失敗自動退回並啟動舊版。
- 服務帳號 `projectapp` 不存在會自動建立（system、nologin、無家目錄）。
- v1.000.0 有手改過的檔（例如 `auth.py` 快取），只要是已知可還原的，會先備份到 `/root/projectapp-handfix/<時間>/`（畫面印出路徑）、還原原版再接手；其他差異照舊拒絕並列出檔名。
- log 固定在 `/var/log/projectapp/patch_<時間>.log`；完整安裝包留在 `/root/projectapp-bundle/`，日後維運指令用它。
- 全新機第一次要先回答 AD 等設定：把包解到 /tmp 後 `sudo bash /tmp/patch_projectapp_<版本>_<時間>/patch.sh --setup`，完成後再跑一次 up-patch。只想看會走哪條路：`patch.sh --check`（什麼都不改）。
- 詳細行為、結束碼與事後退回：安裝包內 `README_INSTALL.md`「升級」一節。

2.001.0 以前的舊做法（`patch/YYYYMMDD/` 修補包、Releases 的 `full-<版本>` 完整包）保留在原處供查閱，新版不再用。

> 狀態：**測試候選，不是正式核准**。審查由 Google Gemini（Antigravity）替補擔任，執行紀錄在作者機器產生，只能核對一致性、無法排除偽造；正式部署須使用者本人批准。驗證範圍：AlmaLinux 9 容器（v1.000.0 情境把 os-release 模擬成 RHEL 9.6）；**真正的 RHEL 9.6 機器（公司測試機）還沒驗證**。

## 本機試用（Linux）

下列命令從專案根目錄執行，需要網路下載依賴。Linux 須具備 Python 3.11+、venv 及 pyodbc 所需的系統 ODBC runtime；本儲存庫未附 Python、系統套件或離線 wheels。

```sh
cd product
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e '.[test]'
mkdir -p var
export APP_MODE=test
export DATABASE_URL="sqlite:///$PWD/var/workboard.sqlite"
export WORKBOARD_STORAGE="$PWD/var/attachments"
export WORKBOARD_HOSTS=localhost,127.0.0.1
python -m alembic upgrade head
python -m flask --app workboard.app:create_app seed-test-identities
python -m flask --app workboard.app:create_app run --host 127.0.0.1 --port 3333
```

瀏覽 `http://127.0.0.1:3333/`。測試模式使用合成人員選單，不驗證 AD 密碼；僅限隔離環境，勿放入真實資料或直接開放到公網。上方命令使用開發伺服器，不適用正式服務。

## 測試

```sh
cd product
python -m pytest tests -q
```

測試使用暫存 SQLite 與虛構資料。通過不代表正式 AD、SQL Server、公司儲存、病毒掃描或備份還原整合通過。SQL Server 離線 DDL 編譯測試也不是連線驗證。

## 正式環境與發佈狀態

正式模式要求 SQL Server 及 LDAPS，缺少設定時拒絕啟動或登入，不會自動退回測試登入。設定欄位見 `product/deploy/production.env.example`；請自行在受保護的環境設定，勿將帳密提交到 Git。

正式 Release 尚未發佈，操作正式主機未獲授權。使用者已授權安裝包準備及通過關卡後的發佈；須完成安裝、重跑、升級回復、外部審查及交付關卡後才可發佈。

## 公開範圍

只包含應用程式、資料庫遷移、合成資料測試及通用文件。原環境的 IP、SSH 別名、部署腳本、帳密、資料庫、附件、真實人員資料、內部需求文件、AI 對話及完整審查證據均未上傳。預設允許的主機僅保留 localhost 與 127.0.0.1，環境位址由 WORKBOARD_HOSTS 設定。

公開來源與內部測試部署有去識別化差異，不以公開提交直接宣稱部署版本一致。此處不提供正式版通過標記；公開上傳不替代驗收。

本儲存庫尚未指定開源授權；公開可讀不表示另行授予使用或再散布授權。
