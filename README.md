# 線上考試系統

學生輸入學號進場作答（單選、多選、填充），每題即時存到伺服器；時間到由伺服器用已存答案自動代交。老師後台管理名單、題庫、考試，看考場即時狀態、各題分析並匯出成績。

- 學生端：`/`
- 老師後台：`/admin`
- 設定考場電腦代碼：`/setup?pc=A01`

## 本機開發（MacBook）

```bash
uv venv .venv
uv pip install -p .venv -r requirements-dev.txt
cp .env.example .env              # 改 ADMIN_PASSWORD、SECRET_KEY，IP_HEADER 留空
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest -q     # 跑測試
```

資料存在 `data/exam.db`（SQLite，已在 `.gitignore`）。

## 部署到伺服器（Docker）

```bash
git clone <repo> exam && cd exam   # 之後更新用 git pull
cp .env.example .env               # 填 ADMIN_PASSWORD、SECRET_KEY、IP_HEADER 等
docker compose up -d --build       # 只開 app，http://localhost:8000（.env 的 APP_PORT 可改）
```

對外公開二選一：

| 方式 | 指令 | `.env` 設定 | 適合 |
| --- | --- | --- | --- |
| Cloudflare Tunnel | `docker compose --profile tunnel up -d --build` | `IP_HEADER=cf`、`TUNNEL_TOKEN=...` | Mac mini 放家裡，不用開路由器 port |
| Caddy 自動 HTTPS | `docker compose --profile caddy up -d --build` | `IP_HEADER=xff`、`DOMAIN=exam.你的網域` | VPS，DNS A 紀錄指到主機 IP |

Cloudflare Tunnel：在 Cloudflare Zero Trust 建一個 tunnel，Public Hostname 設 `exam.你的網域` → Service `http://app:8000`，把 token 貼到 `.env`。

也可以用 CLI 建（不用進 Dashboard，compose 已用 `TUNNEL_URL` 指到 `app:8000`）：

```bash
cloudflared tunnel create exam-system
cloudflared tunnel route dns exam-system exam.你的網域
cloudflared tunnel token exam-system     # 輸出貼到 .env 的 TUNNEL_TOKEN
```

若 `~/.cloudflared/config.yml` 裡已有別的 `tunnel:`，`route dns` 會被它蓋過而綁錯 tunnel，要另建一個只寫 `tunnel: <UUID>` 的設定檔，用 `--config` 指定它（`/dev/null` 會被拒絕）。

更新版本：

```bash
git pull
docker compose --profile tunnel up -d --build   # 依你用的 profile
```

備份（考前考後各一次，平常每日）。要在容器裡做，不要從主機直接開 `data/exam.db`（Docker Desktop 的掛載資料夾不支援跨系統共用 SQLite 鎖）：

```bash
docker compose exec app python -c "import sqlite3,time; sqlite3.connect('/app/data/exam.db').backup(sqlite3.connect(time.strftime('/app/data/backup-%Y%m%d-%H%M.db')))"
```

壓力測試（考前確認主機撐得住；會建立測試學生與考試，請對另開的測試容器執行，不要打正式站）：

```bash
docker build -t exam-loadtest . && docker run -d --rm --name exam-loadtest -p 127.0.0.1:8013:8000 \
  -e ADMIN_PASSWORD=loadtest -e SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))") exam-loadtest
.venv/bin/python scripts/loadtest.py --url http://127.0.0.1:8013 --password loadtest \
  --students 100 --questions static/samples/questions.csv          # 加 --auto-submit 測時間到代交
docker stop exam-loadtest
```

## IP 設定很重要

`IP_HEADER` 決定伺服器相信哪個 header 當學生真實 IP，要跟部署方式一致：

- `cf`：經過 Cloudflare Tunnel
- `xff`：經過 Caddy
- 空白：直連（本機開發）

設錯的話，考場 IP 白名單要嘛擋掉所有人，要嘛可以被學生偽造 header 繞過。部署後到後台按「加入我現在的 IP」，確認顯示的是學校的對外 IP。

## 資安注意事項

- **正式考試請設定「允許進場的 IP」**：學生預設只要學號就能登入，學號通常是連號，知道學號的人就能冒名作答。設了 IP 白名單後，開始作答、存答案、交卷都必須在考場網路。
- 後台密碼錯 10 次會鎖該 IP 15 分鐘；學生密碼錯 5 次會鎖「該 IP＋學號」5 分鐘；同一 IP 5 分鐘內輸入 60 個不存在的學號會被暫擋。鎖定紀錄存在記憶體，重啟容器即清除。
- 學生改密碼或老師重設密碼，該學生舊的登入立即失效；更換 `ADMIN_PASSWORD` 後老師需重新登入。
- 有安全標頭（CSP、禁止被 iframe 嵌入），經 Cloudflare 的 http 連線會自動轉到 https。
- 成績 CSV 會把 `= + - @` 開頭的欄位前面加 `'`，避免 Excel 執行公式。

## CSV 格式

名單：後台「名單」可以直接上傳學校的成績登記表（`.xlsx`），系統會在每個工作表裡找含「學號」「姓名」的表頭列，只讀這兩欄。也可以上傳 CSV（`static/samples/students.csv`）：

```
學號,姓名
11201,王小明
```

目前介面以單一班級為主，班級欄位不用填。

題庫 `static/samples/questions.csv`：欄位 `type,stem,code,A,B,C,D,E,F,answer,explanation,points,multi_mode,tags`

| type | answer 寫法 |
| --- | --- |
| `single` | `B` |
| `multi` | `A;C`（`multi_mode`：`all` 全對才給分、`partial` 學測式部分給分） |
| `fill` | 格與格用 `;` 分隔，同一格多種寫法用 `\|`，例如 `臺北\|台北;高雄` |

填充題比對時會去頭尾空白、全形轉半形、不分大小寫，數字 `0.5` 與 `1/2` 視為相同。

## 學生密碼

- 匯入名單後學生沒有密碼，只輸入學號就能登入；登入後頁面會提醒設定密碼，也可以從右上角「修改密碼」更改。
- 設定密碼後登入要輸入學號和密碼，密碼正確前不會顯示姓名。同一學號 5 分鐘內錯 5 次會暫時鎖定 5 分鐘。
- 學生忘記密碼時，老師在後台「名單」按「重設密碼」，就會恢復成只輸入學號即可登入。
- 密碼以 scrypt 加鹽雜湊存放；重新匯入名單不會清掉已設定的密碼。

## 作答保存與代交

- 選擇題點選後 0.2 秒、填充題停止輸入 0.8 秒或離開輸入框時存到伺服器；失敗每 3 秒重試，關分頁時用 keepalive 送出。
- 伺服器背景每 5 秒檢查一次，超過時間（含 30 秒網路寬限）的作答一律用已存答案代交，不依賴學生瀏覽器。
- 重新整理或換電腦，用同一學號再進場會接續原本的作答與剩餘時間。
