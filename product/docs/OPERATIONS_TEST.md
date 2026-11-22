# 隔離測試環境操作維護

版本：0.1.0 開發中；公開通用文件。僅適用合成資料測試，正式整合與安裝包尚待驗證。

啟動與測試命令見根目錄 README。測試模式不驗證密碼，禁止接入真實資料或直接對公網服務。

具維護權限者可查詢磁碟可用空間與附件補償待辦數。讀取失敗時顯示未知，不能當成零；資料夾不存在時表示尚未建立待辦。此畫面是查詢時的快照，不是背景監控系統。

附件補償只核對已登錄待辦，在相同受控環境執行：

```sh
python -m flask --app workboard.app:create_app reconcile-uploads --limit 100
```

它會取得資料庫鎖並保留已提交版本；無法確認時保留待辦。不要自行刪除附件目錄或資料庫。

合成資料的備份與隔離還原命令：

```sh
python -m flask --app workboard.app:create_app backup-test /absolute/new-backup-directory
python -m flask --app workboard.app:create_app restore-test /absolute/new-backup-directory /absolute/new-restore-directory
```

目的地使用新的空路徑，還原後須另行驗證資料與附件，勿覆蓋正在使用的資料。本命令不是公司正式備份方案；正式 SQL Server、儲存、權限、加密、保留政策與災難復原仍須實機演練。

升級前須有已驗證備份。資料庫降版不提供自動破壞式回復；失敗時保持原資料，依已演練的成對資料庫與附件備份回復。正式維運前必須另行核准部署、監控及回復程序。
