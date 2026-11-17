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
docker compose up -d --build       # 只開 app，http://localhost:8000
```

對外公開二選一：

| 方式 | 指令 | `.env` 設定 | 適合 |
| --- | --- | --- | --- |
| Cloudflare Tunnel | `docker compose --profile tunnel up -d --build` | `IP_HEADER=cf`、`TUNNEL_TOKEN=...` | Mac mini 放家裡，不用開路由器 port |
| Caddy 自動 HTTPS | `docker compose --profile caddy up -d --build` | `IP_HEADER=xff`、`DOMAIN=exam.你的網域` | VPS，DNS A 紀錄指到主機 IP |

Cloudflare Tunnel：在 Cloudflare Zero Trust 建一個 tunnel，Public Hostname 設 `exam.你的網域` → Service `http://app:8000`，把 token 貼到 `.env`。

更新版本：

```bash
git pull
docker compose --profile tunnel up -d --build   # 依你用的 profile
```

備份（考前考後各一次，平常每日）：

```bash
sqlite3 data/exam.db ".backup data/backup-$(date +%Y%m%d-%H%M).db"
```

## IP 設定很重要

`IP_HEADER` 決定伺服器相信哪個 header 當學生真實 IP，要跟部署方式一致：

- `cf`：經過 Cloudflare Tunnel
- `xff`：經過 Caddy
- 空白：直連（本機開發）

設錯的話，考場 IP 白名單要嘛擋掉所有人，要嘛可以被學生偽造 header 繞過。部署後到後台按「加入我現在的 IP」，確認顯示的是學校的對外 IP。

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

## 作答保存與代交

- 選擇題點選後 0.2 秒、填充題停止輸入 0.8 秒或離開輸入框時存到伺服器；失敗每 3 秒重試，關分頁時用 keepalive 送出。
- 伺服器背景每 5 秒檢查一次，超過時間（含 30 秒網路寬限）的作答一律用已存答案代交，不依賴學生瀏覽器。
- 重新整理或換電腦，用同一學號再進場會接續原本的作答與剩餘時間。
