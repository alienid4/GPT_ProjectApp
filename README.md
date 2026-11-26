# 工作管理平台 — 開發中來源

版本：0.1.0。此儲存庫是去除環境資訊的開發來源快照，**不是已驗收的正式版本，也不是完整離線一鍵安裝包**。

提供個人工作看板、多層專案與 WBS、甘特圖、成果確認、附件版本、討論、權限、管理及匯出功能。採 Python 3.11+、Flask、SQLAlchemy 與 Alembic。

## 已安裝的機器要更新 → 看 `patch/`

`patch/YYYYMMDD/patch_gpt_projectapp_YYYYMMDD_v<版本>.tar.gz` 是給**已經裝好第二版的機器**用的更新包（只含變更檔，幾百 KB，不必重抓整包）。

- **永遠套版本最大的那一包就好**：看檔名最後的 `v<版本>`，**照數字比，不要照文字比**（例：`2.000.30` 比 `2.000.9` 新；版本格式是「大版本.小功能.小功能修補」）。版本相同取日期較晚的。
- 每一包都是**累積包**：從包內 `copy_commands.md` 寫的基準版本（`BASE_VER`）一路到該版本，中間漏掉的不用補。比基準版本舊的機器會被 `patch.sh` 拒絕，請改用完整包。
- **從 v1.001.0 升級請用完整包**（見本儲存庫的 Releases 頁面，例如 `full-2.000.30`；第二版執行環境有變，修補包不適用）。

目前最新：`patch/20261126/patch_gpt_projectapp_20261126_v2.001.0.tar.gz`（累積 2.000.30 → 2.001.0；更早的 2.000.28 機器先套 `v2.000.30` 那包，或直接用完整包）。

```bash
cd /tmp && sha256sum -c patch_gpt_projectapp_YYYYMMDD_v<版本>.sha256
sudo mkdir -m 700 /root/projectapp-patch
sudo tar -xzf patch_gpt_projectapp_YYYYMMDD_v<版本>.tar.gz -C /root/projectapp-patch
sudo bash /root/projectapp-patch/ProjectApp_v<版本>-patch_*/patch.sh
```

同目錄的 `.sha256` 就是第一行核對用的。`patch.sh` 會先備份、失敗自動退回；重複執行會回報「已是新版」，不會壞。詳細結束碼與退回方式見包內 `copy_commands.md`、`ROLLBACK.md`。
修補包超過 50 MB 時不放進本儲存庫，改放在 Releases 頁面同版本的附件。

> 狀態：**測試候選，不是正式核准**。審查由 Google Gemini（Antigravity）替補擔任，執行紀錄在作者機器產生，只能核對一致性、無法排除偽造；正式部署須使用者本人批准。

## 從 v1.000.0（`/opt/it-workboard`）升級 → 用完整包原地接手，不能用修補包

v1.000.0 是舊的單一目錄配置，`patch.sh` 會拒絕（什麼都不改）。請從 Releases 下載最新的完整升級包（例如 `full-2.001.0`），先核對 sha256 再執行：

```bash
sha256sum -c ProjectApp_v<版本>-full_<時間>.tar.gz.sha256
sudo mkdir -m 700 /root/projectapp-bundle
sudo tar -xzf ProjectApp_v<版本>-full_<時間>.tar.gz -C /root/projectapp-bundle
B=/root/projectapp-bundle/ProjectApp_v<版本>-full_<時間>
sudo bash $B/INSTALL.sh --production --check                 # 只列出接手計畫，不改任何東西
sudo bash $B/INSTALL.sh --production --install --adopt-v1000
```

- 資料庫與附件留在原位，只換程式；舊目錄 `/opt/it-workboard` 全程不讀寫，留作回退依據；失敗或中斷會自動回到舊版（中斷時重跑同一指令，或 `INSTALL.sh --production --recover`）。
- 接手後一旦有人寫入新資料，就不能再自動退回 v1.000.0，要退只能用配對備份還原。
- 驗證範圍：只在「AlmaLinux 9 容器、os-release 模擬成 RHEL 9.6」測過；**真正的 RHEL 9.6 機器還沒驗證**。

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
