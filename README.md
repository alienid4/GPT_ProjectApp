# 工作管理平台 — 開發中來源

版本：0.1.0。此儲存庫是去除環境資訊的開發來源快照，**不是已驗收的正式版本，也不是完整離線一鍵安裝包**。

提供個人工作看板、多層專案與 WBS、甘特圖、成果確認、附件版本、討論、權限、管理及匯出功能。採 Python 3.11+、Flask、SQLAlchemy 與 Alembic。

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
