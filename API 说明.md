# AV_Wall API 说明

供其它程序调用 AV_Wall 海报墙服务的接口清单。

- **服务地址**：默认 `http://127.0.0.1:8787`（双击 `启动.bat` 启动）
- **局域网地址**：`http://<本机IP>:8787`（启动横幅里会打印，页面「📱 局域网」按钮也有）
- **协议**：HTTP/1.1，请求与响应均为 `application/json; charset=utf-8`（静态资源与媒体流除外）
- **版本**：v1.7.0 起新增 `/api/root`（媒体根 / 指定目录）相关接口；v1.8.0 起服务**只能手动停止**

---

## 〇、v1.8.0 安全与健壮性变更速览

| 项 | v1.7.0 | v1.8.0 |
|---|---|---|
| CORS | `Access-Control-Allow-Origin: *` | 只回显请求方自己的 `Origin`（等效不跨域） |
| 写操作门禁 | 仅 localhost | localhost **+ 同源 + Host 指向本机**（防 DNS 重绑定） |
| `POST /api/scan` | 无门禁、同步执行 | 仅本机、**异步触发**（202 + 轮询 status） |
| `config.json` | 可经 `/avwall/config.json` 直接下载 | **403 禁止访问** |
| 媒体流 | 先发 `Content-Length` 再开文件 | **先开文件再发头**；读不满则强制断连（不再永久挂死） |
| 目录校验错误文案 | 分别提示「不存在/不是目录/不可读」 | **统一一句话**（防目录存在性 oracle） |
| 请求体 | 无上限 | 上限 1MB，超限拒绝并断连 |
| 缩略图 | 无大小上限 | 上限 32MB，超限 413 |
| 扫描全失败 | 写出 count=0 的空库覆盖真实数据 | **报错退出（码 3）且不写任何文件** |
| 连接读超时 | 无 | 30 秒（防「只连不发」占线程） |
| 端口占用 | 第二个实例**静默抢占**同一端口（Windows `SO_REUSEADDR` 语义），两实例分流请求 | **第二个实例拒绝启动**（退出码 3 + 中文提示）；`allow_reuse_address = False` |

---

## 一、权限模型（重要）

| 类别 | 接口 | 局域网可用 | 本机（localhost）可用 |
|---|---|---|---|
| **只读** | `GET /api/*` | ✅ | ✅ |
| **写操作** | `POST /api/root`、`POST /api/config`、`POST /api/scan`、`POST /api/shutdown` | ❌ 403 | ✅ |

写操作会从**客户端 IP** 判定是否 localhost（`127.0.0.1` / `::1`）。
手机、平板通过局域网 IP 访问时调用写接口会得到 `403`，响应体形如：

```json
{"error": "media root change only allowed from localhost"}
```

> 设计意图：避免手机误触导致服务指向不存在的目录、关不掉服务。

### 跨站请求防护（v1.8.0 起）

写操作额外要求**同源**：`Origin` 头缺省（命令行 / 脚本调用）一律放行；
`Origin` 存在时必须与 `Host` 同源，否则返回 `403`：

```json
{"error": "cross-origin request rejected"}
```

> 设计意图：防止局域网里的手机打开恶意网页时，被诱导发跨站请求改目录或关服务。

### DNS 重绑定防护（v1.8.0 起）

`Host` 头也必须指向**本机**才允许写操作：`127.0.0.1` / `localhost` / `::1` /
`0.0.0.0`，或本机任一局域网 IP（含本机主机名）。

```json
{"error": "cross-origin request rejected"}
```

> 设计意图：只校验 `Origin` 不够 —— 攻击者可以把域名解析到 `127.0.0.1`
> （DNS 重绑定），此时 `Host` 与 `Origin` 都是攻击者的域名、**彼此同源**，
> 却已经指向本机。必须再校验 `Host` 确实是本机地址。

### 响应体上限

| 位置 | 上限 | 超限行为 |
|---|---|---|
| 请求体（`POST` 的 `Content-Length`） | 1MB | 拒绝并关闭连接 |
| 缩略图（`/thumb/*`） | 32MB | `413 {"error": "thumbnail too large"}` |
| 静态资源（`/avwall/*`、`/data/*`） | 64MB | `413 {"error": "static file too large"}` |

### 禁止直接访问的文件

`/avwall/config.json` 与 `/avwall/library.meta.json` 返回 `403`：
前者是服务端配置，后者含 NAS 绝对路径与缓存内容，都不该被静态路由暴露。

### 跨域读取

服务端**不再**发送 `Access-Control-Allow-Origin: *`。有 `Origin` 头时只回显
请求方自己的 `Origin`（等价于同源策略），浏览器里的别的网站无法读本服务的数据。

---

## 二、媒体根（指定目录）—— 新增

### 1. 读取当前媒体根

```
GET /api/root
```

响应 `200`：

```json
{
  "ok": true,
  "path": "\\\\ds1821\\HD\\Download\\99 AV",
  "content_dirs": ["B1", "B2", "下载完成1", "下载完成2", "下载完成3"],
  "default": "\\\\ds1821\\HD\\Download\\99 AV"
}
```

| 字段 | 说明 |
|---|---|
| `path` | 当前生效的媒体根（UNC 绝对路径） |
| `content_dirs` | 该目录下实际存在的内容目录（`B1` / `B2` / `下载完成1~3`） |
| `default` | 默认媒体根（程序内置，可用 `POST` 传它来恢复） |

### 2. 校验某个目录是否可用（不修改任何状态）

```
GET /api/root?path=<URL 编码后的路径>
```

**目录可用** → `200`：

```json
{
  "ok": true,
  "valid": true,
  "error": null,
  "path": "\\\\ds1821\\HD\\Download\\99 AV",
  "content_dirs": {"B1": 127, "B2": 1, "下载完成1": 176, "下载完成2": 40, "下载完成3": 25}
}
```

**目录不可用** → `400`：

```json
{
  "ok": false,
  "valid": false,
  "error": "目录不可用（不存在 / 不是目录 / 不可读 / 没有内容目录）"
}
```

校验规则（v1.8.0 起错误文案**统一**，避免用不同文案泄露目录是否存在）：

| 情况 | `error` |
|---|---|
| 空字符串 | `目录不能为空` |
| 相对路径 | `必须是绝对路径（UNC 如 \\server\share 或盘符如 D:\...）` |
| 不存在 / 不是目录 / 不可读 / 没有内容目录 | `目录不可用（不存在 / 不是目录 / 不可读 / 没有内容目录）` |

> v1.8.0 说明：原先会分别返回「目录不存在：X」「不是目录：X」「目录不可读：X」，
> 攻击者可用文案差异**逐个探测任意路径是否存在**（目录存在性 oracle）。
> 现在这四种情况统一为一句话，只有**空**与**相对路径**仍单独提示
> （这两种是调用方自己的格式问题，不泄露目标是否存在）。

### 3. 设置媒体根（仅 localhost）

```
POST /api/root
Content-Type: application/json

{"path": "\\\\ds1821\\HD\\Download\\99 AV"}
```

成功 → `200`：

```json
{
  "ok": true,
  "saved": true,
  "path": "\\\\ds1821\\HD\\Download\\99 AV",
  "content_dirs": {"B1": 127, "B2": 1, "下载完成1": 176, "下载完成2": 40, "下载完成3": 25},
  "message": "媒体根已切换，正在后台重新扫描"
}
```

失败（目录无效）→ `400`；非本机调用 → `403`。

**行为说明**：
1. 先校验目录（不通过直接 400，**不会修改**任何状态）；
2. 写入 `config.json` 的 `media_root`；
3. **实时切换**运行中的服务（`/media`、`/thumb` 立即指向新目录）；
4. **自动触发一次后台重新扫描**（异步，不阻塞响应）。切到新目录时首次扫描可能是全量的（耗时较长），可轮询 `GET /api/status` 的 `scan` 字段确认完成。

> **注意**：切换到**不同**的目录会触发全量重扫（旧缓存的 `root` 不再匹配）。
> 仅大小写不同（如 `\\Ds1821\hd\...` → `\\ds1821\HD\...`）**不会**，因为缓存比对
> 是大小写不敏感的。
>
> **v1.8.0 修正**：原先切换时触发的后台重扫**可能被静默丢弃**——切根调
> `run_async`，但那次抢不到扫描锁（5 秒超时）就返回 "scan already running"，
> 异常又被吞掉，接口却照样回 `200 {"rescanning": true}`，新根从未被扫过，
> `config.json` 与 `library.json` 的根目录永久对不上。
> 现在扫描期间若媒体根被改过，会**自动补扫一次**（最多 3 次）。
> 响应里新增 `previous_path` / `same_directory` / `rescanning` 三个字段。

---

## 三、配置

### 读取配置

```
GET /api/config
```

```json
{
  "ok": true,
  "media_root": "\\\\ds1821\\HD\\Download\\99 AV",
  "default_media_root": "\\\\ds1821\\HD\\Download\\99 AV",
  "stop_mode": "manual",
  "config": {
    "media_root": "\\\\ds1821\\HD\\Download\\99 AV"
  }
}
```

> `stop_mode` 固定为 `manual`：v1.8.0 起服务**只能手动停止**（`Ctrl+C` 或 `POST /api/shutdown`），
> 「关闭页面即停服」整套机制已删除。

### 修改配置（仅 localhost）

```
POST /api/config
Content-Type: application/json

{"media_root": "\\\\ds1821\\HD\\Download\\99 AV"}
```

响应：

```json
{
  "ok": true,
  "saved": true,
  "path": "\\\\ds1821\\HD\\Download\\99 AV",
  "previous_path": "\\\\ds1821\\HD\\Download\\99 AV",
  "same_directory": true,
  "rescanning": false,
  "content_dirs": {"B1": 127, "B2": 1, "下载完成1": 176, "下载完成2": 40, "下载完成3": 25},
  "message": "媒体根写法已更新（同一目录，无需重扫）"
}
```

- `media_root`：媒体根（UNC 绝对路径），与 `POST /api/root` 完全等价。
- 改动**即时生效**（不需要重启），并落盘到 `config.json`。
- 传的是**同一目录的不同写法**（如大小写不同）时，`same_directory: true`，
  只更新数据文件里的 `root` 字段，**不触发重扫**。

### 已下线的配置键（v1.8.0）

以下三个键**不再可写**，传入会返回 `400` 并说明原因（不静默忽略）：

| 键 | 原因 |
|---|---|
| `auto_shutdown_on_page_close` | 「关页面即停服」整套机制已删除 |
| `keepalive_timeout_sec` | 保活看门狗已删除 |
| `watchdog_interval_sec` | 保活看门狗已删除 |

```json
{
  "error": "auto stop-on-page-close was removed in v1.8.0; the service now stops only on Ctrl+C or POST /api/shutdown",
  "removed_keys": ["auto_shutdown_on_page_close"]
}
```

---

## 四、数据与状态

### 完整库数据

```
GET /api/library
```

返回 `data/library.json` 的全部内容，结构：

```json
{
  "version": 1,
  "root": "\\\\ds1821\\HD\\Download\\99 AV",
  "generated": "2026-10-22T01:20:00+08:00",
  "count": 369,
  "total_videos": 436,
  "total_posters": 365,
  "total_shots": 128,
  "items": [
    {
      "id": "ABF-114",
      "src": "B1",
      "key": "B1/ABF-114",
      "poster": "B1/ABF-114/photo_2026-06-29_01-26-29.jpg",
      "shots": ["B1/ABF-114/photo_2026-06-29_01-26-57.jpg"],
      "videos": [
        {
          "path": "B1/ABF-114/ABF-114-UC.mp4",
          "name": "ABF-114-UC.mp4",
          "size": 3650831910,
          "mtime": 1782671789.3942664,
          "mtime_iso": "2026-06-29T02:36:29"
        }
      ],
      "vcount": 1,
      "has_video": true
    }
  ],
  "warnings": []
}
```

> `path` / `poster` / `shots` 都是**相对于媒体根**的相对路径。
> 拼完整 URL：`<服务地址>/media/<path>`（视频，支持 Range）、`<服务地址>/thumb/<path>`（图片）。

### 服务状态

```
GET /api/status
```

```json
{
  "ok": true,
  "version": "1.8.0",
  "root": "\\\\ds1821\\HD\\Download\\99 AV",
  "library_exists": true,
  "scan": {
    "running": false,
    "ok": true,
    "scanner_exists": true,
    "error": null,
    "output_tail": "扫描完成: 番号 369 个 / 视频 436 个 ...",
    "finished_at": "2026-10-22 09:30:00"
  }
}
```

> `scan.finished_at` 在**扫描尚未完成**时是空字符串 `""`（启动后的首次扫描期间即如此）。
> `scan.ok` 为 `null` 表示还没跑过；`false` 表示失败，此时 `scan.error` 有原因。

### 局域网信息 + 二维码

```
GET /api/info
```

```json
{
  "ok": true,
  "lan_ip": "***已脱敏***-内网IP",
  "port": 8787,
  "url": "http://***已脱敏***-内网IP:8787/",
  "qr": [[0,1,1,0,0,...], ...]
}
```

`qr` 是 0/1 二维码矩阵（本机没装 `qrcode` 库时为 `null`）。

### 触发扫描（仅 localhost，v1.8.0 起异步）

```
POST /api/scan
```

**异步触发**一次增量扫描，**立刻返回** `202`：

```json
{
  "ok": true,
  "started": true,
  "message": "scan started; poll GET /api/status for progress"
}
```

扫描已在进行中 → `409 {"ok": false, "error": "scan already running"}`。

> **v1.8.0 变更（重要）**：
> 1. 原先**没有任何门禁**——局域网里任何设备都能触发扫描，而扫描最坏要跑
>    30 分钟（`SCAN_TIMEOUT_SEC`），等于任何人都能把服务占死。现在仅本机可调。
> 2. 原先**同步执行**，HTTP 请求会一直挂着直到扫描结束；客户端一关就成僵尸
>    请求，服务端线程还占着。现在异步触发 + 立刻返回，进度用
>    `GET /api/status` 的 `scan` 字段查（`scan.running` / `scan.ok` / `scan.error`）。
>
> 页面「⟳ 扫描」按钮已同步改为**轮询 `/api/status`** 等扫描真正结束再刷新
> （原先固定 3 秒后刷新，扫描要几分钟，刷新了也看不到新数据）。

### 健康检查

```
GET /api/ping
```

`{"ok": true}`

---

## 五、生命周期

| 接口 | 权限 | 说明 |
|---|---|---|
| `POST /api/keepalive` | 仅 localhost | **兼容占位**：返回 `200` 但什么都不做（旧脚本不会收到 404） |
| `POST /api/shutdown` | 仅 localhost | 优雅停止服务，响应 `{"ok": true, "message": "shutting down"}` |

### 停止服务（v1.8.0 起只有两条途径）

1. 在运行服务的命令行窗口按 `Ctrl+C`；
2. 本机调用 `POST /api/shutdown`（局域网地址无权调用）。

> **已删除**：原先「页面每 5 秒发一次 keepalive → 服务端 12 秒没收到就自动停服」
> 的整套机制。原因是过于脆弱 —— 手机锁屏、切到后台、弱网重连、浏览器后台节流
> 定时器，都会被误判成「页面已关闭」而把服务杀掉。
> 页面现在**不再**发保活请求；`config.json` 里与自动停服相关的三个键也已移除。

### 端口占用（v1.8.0 起）

同一个端口**只允许一个实例**。重复双击 `启动.bat` 时：

- 启动脚本会先尝试结束占用旧端口的进程；若结束不掉（例如旧进程以别的账户运行），
  脚本会明确提示「Port 8787 is still in use」并退出，不会启动第二个实例。
- 即便绕过脚本直接跑 `python server.py --port 8787`，第二个实例也会**拒绝启动**：
  打印「端口 8787 已被占用——可能服务已经启动了。」并以**退出码 3** 结束。

> 背景：Windows 的 `SO_REUSEADDR` 语义与 POSIX 不同，它允许**第二个进程绑定一个
> 已经被 LISTENING 的端口**。因此原先重复启动会出现两个实例同时服务 8787，
> 内核把请求随机分流到两个进程 —— 表现为「时好时坏」「有时候图片加载不出来」。
> 现在 `allow_reuse_address = False`，端口冲突会立刻暴露成一条明确的错误。

---

## 六、调用示例

### PowerShell

```powershell
# 读当前媒体根
(Invoke-RestMethod "http://127.0.0.1:8787/api/root").path

# 校验某个目录
try {
  Invoke-RestMethod "http://127.0.0.1:8787/api/root?path=$([uri]::EscapeDataString('\\ds1821\HD\Download\99 AV'))"
} catch {
  $_.ErrorDetails.Message   # 含 error 字段
}

# 切换媒体根（必须本机调用）
Invoke-RestMethod "http://127.0.0.1:8787/api/root" -Method Post `
  -ContentType "application/json" `
  -Body (@{ path = "\\ds1821\HD\Download\99 AV" } | ConvertTo-Json)

# 等扫描完成
while ($true) {
  $s = Invoke-RestMethod "http://127.0.0.1:8787/api/status"
  if (-not $s.scan.running) { break }
  Start-Sleep -Seconds 2
}
"scan ok = $($s.scan.ok)"
```

### Python

```python
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# 读当前媒体根
print(get("/api/root")["path"])

# 校验目录
probe = "/api/root?path=" + urllib.parse.quote(r"\\ds1821\HD\Download\99 AV")
print(get(probe))

# 切换媒体根（需本机调用）
print(post("/api/root", {"path": r"\\ds1821\HD\Download\99 AV"}))
```

### 局域网调用（只读）

```python
BASE = "http://***已脱敏***-内网IP:8787"
print(get("/api/root"))        # ✅ 读操作局域网可用
post("/api/root", {...})       # ❌ 403，仅本机
```

---

## 七、媒体流（视频 / 图片）

不是 JSON，而是二进制流。

| 路径 | 说明 |
|---|---|
| `GET /media/<相对路径>` | 视频流。支持 `Range`：`206 Partial Content` + `Content-Range` + `Accept-Ranges: bytes`；`HEAD` 只回头 |
| `GET /thumb/<相对路径>` | 图片。支持 `HEAD`，不做 Range |

```python
# 查看视频大小（用 HEAD，不要整段下载）
req = urllib.request.Request(f"{BASE}/media/B1/ABF-114/ABF-114-UC.mp4", method="HEAD")
with urllib.request.urlopen(req, timeout=30) as r:
    print("size =", r.headers["Content-Length"])
    print("type =", r.headers["Content-Type"])
```

> **v1.8.0 修正**：原先媒体流是「先发 `Content-Length` 再开文件」。若文件在
> `stat` 之后消失（NAS 抖动、文件被删），服务端会**先发出完整响应头再抛异常**，
> 客户端收到 `Content-Length: 65536` 却收不到 body，连接又不关闭 →
> 视频卡住转圈、永久挂死。读取中途被截断也是同样症状。
> 现在**先 open 成功才发头**（拿不到文件直接 404，不发任何承诺了长度的头），
> 且实际发送字节数少于声明长度时**强制关闭连接**，客户端立刻感知失败。
