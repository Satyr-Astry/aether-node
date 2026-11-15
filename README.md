# AetherNode

[![ci](https://github.com/Satyr-Astry/aether-node/actions/workflows/ci.yml/badge.svg)](https://github.com/Satyr-Astry/aether-node/actions/workflows/ci.yml)

把本地 AI 服务打包成接口、广播到 P2P 网络、并对请求做路由的桌面程序。

```powershell
git clone https://github.com/Satyr-Astry/aether-node
cd aether-node
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

首次打开会出现三步引导：装推理运行时 → 下一个模型 → 决定是否对外共享。默认只监听
`127.0.0.1`，不对外暴露。

## 一句话说明它解决什么

一台机器的 GPU 闲着，另一台想跑模型但没有显卡；或者你有自己的 OpenAI API，又想让本地
模型和网络里其他节点走同一个入口。AetherNode 把这三类来源——**本机模型、自有 API、网络
内其他节点**——收敛到同一个 `/v1`，按价格与延迟自动选路，并记录每一次调用的 token 消耗。

## 已实现

| 能力 | 状态 |
|---|---|
| HF 模型检索 / 断点续传下载 / llama.cpp 部署 | 已验证 |
| OpenAI 兼容 `/v1`（含 SSE 流式、token 计量） | 已验证 |
| Nostr 广播签名服务广告 + 内置 relay | 已验证 |
| 中继隧道（无公网端点也能被调用） | 已验证 |
| 跨节点路由 + 失败 fallback | 已验证 |
| 按 token 计量的账本、收据、对账、Merkle 结算批次 | 已验证 |
| 抽样复算审计、贡献度换算、代币发放计划 | 已验证 |
| 自实现 Keccak/RLP/ECDSA/EIP-155/ABI，可构造链上交易 | 已验证 |
| 默认安全的鉴权、限流、防重放、并发闸门 | 已验证 |
| PyInstaller 打包产物（能跑完整推理链路） | 已验证 |

详细的验证矩阵、已知风险与下一步优先级见 [STATUS.md](STATUS.md)。

## 明确未实现

这些不是「以后再说」，是我给不出结论的部分：

- **跨机器真实网络验证**：所有双节点测试都在同一台机器上（含非回环 IP），NAT、真实 TLS、
  防火墙行为没有实测。
- **真实链交易**：链上部分是离线向量与自洽性验证，没有一笔交易真正广播过。
- **小时级长稳**：只跑过 5 分钟（600 请求，内存 +2.1%）。
- **Docker 镜像**：Dockerfile 与 compose 在仓库里，本机无 Docker，**未构建过**。
- **安装程序**：`installer.iss` 未编译（本机无 ISCC）。
- **干净机器安装**：打包产物在本机能跑，目标机器缺不缺运行库不知道。

`data/` 里的模型与运行时合计 1.3GB，不进仓库，首次运行需下载。

## 目录

```
aether/            主程序（配置、身份、推理、P2P、账本、EVM、GUI）
contracts/         Solidity 合约（结算锚定、ERC-20、发放器）
tests/             15 套单测 + 8 个端到端脚本
```

## 它做什么

- **自动部署**：从 HuggingFace 检索 GGUF 模型，断点续传下载，用 llama.cpp 拉起本地推理
- **统一接口**：本机与网络内所有节点都收敛到一套 OpenAI 兼容的 `/v1`
- **服务广播**：把本机模型、上下文长度、价格、可达性做成签名广告，投递到 Nostr relay
- **请求路由**：按本机优先 + 网络打分选择节点，失败自动换下一个候选
- **零配置发现**：内置 relay，两台机器只要互相能连上对方的端口就能发现彼此

## 快速开始

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

首次运行会在界面上点「安装 / 修复 llama.cpp」，程序会从 GitHub 拉 Windows CUDA 预编译包
（约 300MB，含 CUDA 运行时），解压到 `data/runtime/llama`。

### 两个已知的环境问题

**1. TLS 中间设备导致 HTTPS 失败。**
如果机器上有企业代理或安全网关，其自签 CA 不在公共信任库里，表现为
`CERTIFICATE_VERIFY_FAILED`（搜索模型和下载 llama.cpp 都会失败）。程序默认严格校验，
不做静默降级。确认环境可信后用环境变量显式放开：

```powershell
$env:AETHER_INSECURE_TLS = "1"
.venv\Scripts\python.exe run.py
```

正确做法是把中间设备的根 CA 导出成 PEM，然后：

```powershell
$env:AETHER_CA_BUNDLE = "C:\path\to\corp-ca.pem"
```

**2. 桌面窗口的三级降级。**
`run.py` 按顺序尝试：pywebview 原生窗口 → Edge/Chrome 的 `--app` 模式（无地址栏的应用窗口，
使用独立 profile，不污染日常浏览器）→ 系统默认浏览器。

`pywebview` 需要 `pythonnet`，在部分环境编译不过。实测本机 Edge 157 的 `--app` 模式工作良好，
观感接近原生桌面程序，因此即使不装 pywebview 也不影响使用。强制走浏览器：

```powershell
.venv\Scripts\python.exe run.py --browser
```

**3. HuggingFace 限流。** 短时间大量请求会返回 429，等几分钟即可。

## 自有 API 纳入路由

除了本机模型和网络节点，还可以把自己配的外部 OpenAI 兼容端点注册成 provider，
让 `/v1` 成为唯一入口：

```
POST /api/providers {"action":"add","name":"my-api","endpoint":"https://api.openai.com",
                     "api_key":"sk-...","models":["gpt-4o-mini"],
                     "price":{"in":0.15,"out":0.6}}
```

优先级是 **本机已加载 > 自有外部端点 > 网络直连 > relay 隧道**。外部端点排在本机之后、
网络之前，因为它由自己配置、可用性最可控；基础分 38（`external_score`），再按出价扣分。

调用它会进账本（`source=external`、`role=consume`），但**不产生对 peer 的债务**——
`peer_credit` 表会跳过 `external:` 前缀，它是你自己的上游成本，不是网络结算对象。
`api_key` 在接口里一律脱敏返回。

配套端点：`GET /api/providers`（列表）、`POST /api/providers` 的 `test` 动作（拉 `/v1/models` 验连通）。

## 安全默认

**默认只监听 `127.0.0.1`**。把 GPU 暴露到网络是显式动作，不是默认行为：

- GUI 或 `POST /api/system/share {"enabled": true}` 开启共享
- 开启时自动生成 32 字符 `api_key` 并落盘——没有「裸端口」这个选项
- 非本地访问 `/api` 与 `/v1` 必须带 `X-Api-Key` 或 `Authorization: Bearer`
- 共享模式下连内置 relay 也要 `?token=<api_key>`，本地回环除外
- 本地回环访问免鉴权：能读 `settings.json` 的人本来就有全部权限，强制 GUI 读密钥只是麻烦

老配置里写死 `api_host=0.0.0.0` 的会自动迁移——保留对外行为，但补上 key 并归位配置字段。

**隧道防护**：

| 项 | 默认 | 说明 |
|---|---|---|
| `relay_rate_limit` | 60 / 分钟 | 每节点发起隧道请求的配额，超限拒绝 |
| `relay_replay_window` | 300 秒 | 同一 `req_id` 在窗口内只接受一次 |
| `tunnel_max_concurrency` | 4 | 目标端并发满时快速回 429，不排队 |
| `api_max_concurrency` | 4 | `/v1` 并发闸门，同样快速失败 |

不排队是刻意的：排队会让所有请求一起变慢再一起超时，快速失败能把压力交还给调用方，
它会去换节点或重试。

## 认证与授权

`auth_mode` 决定用哪种方式校验非本地访问：

| 模式 | 行为 |
|---|---|
| `api_key`（默认） | 静态 Bearer / `X-Api-Key`；本地回环默认免鉴权 |
| `jwt` | HS256 JWT，校验签名与 `exp` / `nbf` / `aud`；**不信任本地** |
| `open` | 全放行，只应在确认只监听回环时用 |

JWT 是零依赖实现（标准库 hmac + base64），不引 PyJWT。刻意只支持 HS256——RS256/ES256
需要非对称密钥管理与 JWKS 拉取，是另一个量级的工程，留作接口扩展点而不是假装支持。

`auth_trust_local` 可以关掉，容器或反向代理后面应当关掉：那时本地回环不再代表"可信用户"。

测试覆盖了 `alg=none` 攻击、payload 篡改、错误 audience、过期与未生效、缺密钥，
以及未知模式回退到 api_key（不静默放行）。

**跨节点直连需要 `peer_keys`**。共享模式下 `/v1` 要求鉴权，而广告里不该放明文密钥，
所以节点之间默认调不通——实测 B 调 A 会拿到 `401 需要有效的 api_key`。要让特定伙伴节点
直连，两边预先交换 key：

```json
{"peer_keys": {"<对方的 node_id>": "<对方的 api_key>"}}
```

路由会在对该 peer 的请求里带上这个 key。没配 key 时应当走 relay 隧道——隧道用节点
签名认证，不需要共享密钥，这也是开放网络的正确形态。**这是设计权衡不是缺陷**：
共享密钥意味着彼此完全信任，而隧道只需要信任对方不会冒充。

**断线与重连**（`tests/e2e_reconnect.ps1`）：

| 阶段 | 结果 |
|---|---|
| 正常 | relay connected，发现 1 个 peer |
| 杀掉 A | B 状态变 `connecting`、`attempts=2`，隧道调用 502 且 3.6 秒返回 |
| A 重启 | B 在 2 秒内自动重连，无需重启 B |
| 重连后调用 | 200，路由 `relay`，1.3 秒 |

这里修掉了一个**真正的 bug**：隧道请求原本广播给所有 relay，而代码收到第一个
「目标节点不在线」就判失败。B 连着自己的 relay、A 的 relay 和三个公共 relay，
自己那个不认识 A 立刻回绝，正确转发的那个根本没等到。改成计数——只有**所有** relay
都拒绝才失败，并且每 2 秒检查一次，全拒时快速返回而不是干等 180 秒。

这个 bug 之前没暴露，是因为早期测试里 A 和 B 恰好共用同一个 relay，那次拒绝对路径不构成干扰。
**测试拓扑太干净会掩盖真实缺陷**。

**故障注入**（`tests/e2e_fault.ps1`）——之前所有测试都跑在「一切正常」上，这个脚本专测坏路径：——之前所有测试都跑在「一切正常」上，这个脚本专测坏路径：

| 注入 | 结果 |
|---|---|
| 正常调用 | 200，0.06s |
| 杀掉上游进程 | 502，2.06s，错误信息含具体原因，不挂起 |
| 上游恢复 | 200，自动恢复，本节点无需重启 |
| 端点改成死端口 | 502，2.05s |
| 端点改回 | 200 |
| 并发 8 中途杀上游 | 200×7 + 502×1，8/8 都有明确响应 |
| 故障后健康检查 | `healthz=200`、`readyz=200`、`inflight=0` |

失败请求会以 `status=error` 入账，不会凭空多出成功记录——汇总里 `consume=2` 对应 1 成功 + 1 失败。

## 身份与备份

`data/identity.json` 是节点的全部身份：网络 ID、账本归属、隧道地址、EVM 账户全部由它派生。
**丢了它等于换了一个节点**，历史账本与应收应付都对不上。

备份走口令加密，不提供明文导出：

```powershell
# 导出
Invoke-RestMethod -Uri "http://127.0.0.1:8737/api/identity/export" -Method Post `
  -ContentType "application/json" -Body '{"password":"你的口令（至少 8 位）"}'

# 校验（不落盘）
Invoke-RestMethod -Uri "http://127.0.0.1:8737/api/identity/verify" -Method Post `
  -ContentType "application/json" -Body '{"backup":{...},"password":"..."}'

# 恢复（会覆盖现有身份，旧文件另存为 identity.json.bak）
Invoke-RestMethod -Uri "http://127.0.0.1:8737/api/identity/import" -Method Post `
  -ContentType "application/json" -Body '{"backup":{...},"password":"..."}'
```

加密用 PBKDF2-HMAC-SHA256（20 万次迭代）+ AES-256-GCM。选 GCM 是因为它带认证标签：
口令错了会明确失败，而不是解出一段垃圾再被当成私钥用。每次导出用新盐，同一身份两次导出的
密文不同。

测试验证了：blob 里不含明文或 base64 形式的私钥、错误口令被拒、密文篡改被拒、版本与缺字段
被拒、往返后公钥/EVM 地址/npub 全部一致。

**恢复的风险**：账本按节点 ID 索引，恢复身份后历史记录仍属于旧节点。API 会明确返回
`previous_node_id` 与警告，但账本是否需要迁移得由人判断，程序不自动改。

## 扩展点

`aether/backends/` 定义了推理后端契约（`InferenceBackend` Protocol）与注册表。
目前只有 llama.cpp 一个实现，但接第二个后端时只需实现 Protocol 再 `register()`，
路由、计量、隧道、GUI 都不用改。

```python
from aether import backends

backends.register("my_backend", MyBackend)   # 实现 start/stop/status/base_url
runtime = backends.get("my_backend")          # 单例，与 API 层共享
```

**这是一个「只留接口、不重构」的决定**：`LlamaRuntime` 早就存在，硬拆成多层只会
在只有一个实现时增加抽象成本。真正该拆的时机是有第二个后端时——那时才知道边界
划在哪里合适。引入 Protocol 立刻抓到一个缺口：`LlamaRuntime` 缺 `name` 属性，
契约检查直接报了出来。

自测里用一个 DummyBackend 验证了「自定义后端可插拔且满足契约」，这样接口没有
名存实亡。

## 部署与运维

### Docker

```powershell
docker build -t aether-node .
docker run -p 8737:8737 -v ${PWD}/data:/data aether-node
# 或起两个节点看它互相发现
docker compose up
```

容器里 `AETHER_SHARE=1` 会强制开启共享并自动生成 `api_key`——从 `docker logs` 里取。
**本机没有安装 Docker，镜像未实际构建过**，Dockerfile 是照标准写的。

### 打包成可发布目录
```powershell
pwsh -File build.ps1
```

产物在 `dist/AetherNode/`，96 个文件约 30MB，双击 `AetherNode.exe` 即可运行。
`build.ps1` 会在构建后自动做冒烟测试：起服务、探 `/healthz`、确认静态界面返回 200。

**实测结果**（`tests/e2e_dist.ps1`）：171 个文件 33MB，exe 启动后

- 版本 0.2.0、静态界面 200、`/healthz` true
- 加载真实模型成功：CUDA 全量卸载 28 层到 RTX 4070 Ti
- 推理返回 200，usage 35+2，账本同步增加

这一步比「能启动」重要得多：它证明打包产物**内部依赖完整**（uvicorn 动态导入、GUI 资源、
CUDA DLL 路径都对），而不只是进程能拉起来。这一步还顺带暴露了一个鉴权 bug——见下面
「认证与授权」一节。

打包踩过一个坑值得记下来：uvicorn 用字符串 `"aether.api.app:app"` 导入应用，
PyInstaller 的静态分析看不到这条路径，第一次构建出来的 exe 启动即报
`No module named 'aether.api'`。修法是让 spec 显式 `collect_submodules("aether")`。
同理 uvicorn 自己的动态导入（`loops.auto`、`protocols.http.auto`、`lifespan.on`）
也要手工列进 hiddenimports。

安装程序脚本已提供：installer.iss（Inno Setup 6）。先用 uild.ps1 生成
dist/AetherNode/，再用 ISCC.exe installer.iss 编译。**本机没有 ISCC，脚本未经编译验证**。
卸载时会保留 data/（模型、账本、身份）——删掉 identity.json 等于让用户永久失去节点身份。

**仍未做**：代码签名、自动替换式更新（只做检查与告知）。

### 探针与指标

| 端点 | 用途 |
|---|---|
| `GET /healthz` | 存活探针，进程能响应即 200 |
| `GET /readyz` | 就绪探针，依赖不可用时返回 503 |
| `GET /metrics` | Prometheus 文本格式，17 项指标 |

`/readyz` 会把 `relay`、`model_loaded` 一起报出来但不作为必需项——relay 连不上或模型没加载
不该让进程被摘除，编排层可以据此决定是否接新流量。必需项只有账本可用。

指标覆盖：运行时就绪、relay 连接数、peers、本地模型数、两个方向的 token 与金额、
中继承载字节、隧道转发次数、被速率限制与重放防护拒绝的次数、审计各判定计数。
手写文本格式，没引 `prometheus_client`——指标项有限，多一个依赖不划算。

### 数据库迁移

账本用 SQLite，schema 靠版本号增量升级（当前 v3）：

| 版本 | 内容 |
|---|---|
| v1 | 基础结构：requests / batches / peer_credit |
| v2 | 审计表 audits 与索引 |
| v3 | requests 增加 relay_bytes 列 |

v1 每次启动都幂等执行——库文件被外部工具改坏或缺表时能自愈；v2 起严格按版本号只跑一次。
老库升级、重复打开、残缺库自愈三条路径都有测试覆盖（	ests/test_migrate.py）。
当前版本可在 GET /api/ledger/summary 的 schema_version 看到。

### 长期运行的资源边界

秒级压测发现不了慢速泄漏。审计了三处「只增不减」的结构并给出确定上界：

| 结构 | 问题 | 修法 |
|---|---|---|
| `tunnel._declared_bytes` | 只在收到 done 时清理，超时/失败路径残留 | 移进 finally |
| `relay._seen` 防重放表 | 低频时不触发 prune，无上界 | 加 5000 条硬上限，丢最旧 |
| `relay._rate` 速率窗口 | 长期不活跃的节点记录不释放 | 超过 1 小时的窗口清理 |
| `downloads.tasks` | 已完成任务一直累积 | 上限 200，丢最老的已完成 |

`tests/test_leaks.py` 用「连续 20 次失败请求后表不增长」这类断言验证，而不是断言单次清理——
前者才对应真实的泄漏形态。

### 压测与并发

	ests/loadtest.py + 	ests/e2e_loadtest.ps1：用 mock 上游压代理层，避免推理本身成为瓶颈。

| 场景 | 配置 | 结果 |
|---|---|---|
| A 闸门收紧 | 闸门=2，并发 20，40 请求 | 8×200 + 32×429，成功请求全部入账 |
| B 高并发 | 闸门=64，并发 20，200 请求 | 200 全成功，账本完全一致，p95 79ms |
| C 顺序 | 闸门=4，20 请求 | 全成功，账本一致 |
| D 真实推理 | 闸门=4，并发 4，12 非流式 + 6 流式 | 全成功，账本一致，流式分块正常，inflight 归零 |

/metrics 里的 ether_inflight 是闸门是否泄漏的直接指标，压测结束后必须回到 0。

D 场景用真实 llama-server 而非 mock，验证代理层在数百毫秒级推理延迟下的行为：
非流式 12 请求合计 470+150 tokens 全部入账、全部路由到本机；流式 6 请求分块正常
（最少 7 块）且同样入账；结束后 inflight 为 0。并发刻意压到 4——单卡、
parallel_slots=2 时高并发只会让请求在 llama-server 内部排队，测出来的是它的
排队策略而不是代理层的问题。

### 结构化日志
事件总线会把每个事件同时写进 `data/logs/events.jsonl`，一行一条：

```json
{"seq": 2, "kind": "p2p.started", "ts": 1794670931.87, "payload": {"relays": ["ws://127.0.0.1:18995/relay"], "node_id": "79d9e743..."}}
```

事件本来就带 `kind / ts / payload`，天然是可查询记录，不需要另造日志格式。写失败会自动停用，
不会拖累主流程。

### 仓库

已完成 `git init` 与首次提交，71 个文件、`.git` 约 205KB。`data/`、`.venv/`、`*.db` 与
所有测试临时目录都在 `.gitignore` 里。

**推送需要你自己做**——我没有你的远端地址与凭证：

```powershell
git remote add origin <你的仓库地址>
git push -u origin main
```

CI 在 `.github/workflows/ci.yml`：Windows runner 上跑 ruff、六套测试（逐个执行，因为它们是
靠退出码报告的独立脚本，不走 pytest 收集）、以及前端 JS 与 Python 语法检查。

## 界面

六个视图：

- **模型库** — 搜索 HF、选量化、看下载进度、管理本地模型
- **运行时** — 当前模型参数（GPU 层数、上下文）、llama-server 实时日志
- **网络** — 节点身份、relay 连接、发现的 peers、统一模型目录、广播开关
- **账本** — 计量汇总、对端信用、流水、结算批次、抽样复算、贡献与发放、链上载荷
- **对话** — 直接测试，并显示本次请求实际路由到了哪个节点
- **设置** — 共享开关、20 个配置字段、自有外部 API 管理

交互细节：顶栏有实时连接状态（SSE 断开时会显示「已断开，重连中」）；长任务与首屏
用骨架屏占位；Ctrl+1..9 切换视图、Ctrl+Enter 发送、Esc 清空提示；
窗口窄于 1100px 与 860px 时分别收敛栅格与顶栏；请求带 30 秒超时并在失败时把
HTTP 状态一并报出来，便于区分是网络不可达还是服务报错。

**首次启动引导**：空目录首次打开时，模型库顶部会出现三步清单（装运行时 → 下模型 →
决定是否共享），每步带完成状态与直达按钮。判定条件是「没有本地模型、没有外部端点、
且未手动跳过」——配置了任意一个可用上游后自动不再显示。

## 中继隧道：没有公网的两台机器怎么互相调用

这是最初那个问题的直接答案。`reachability=relay-only` 的节点没有可直达的端点，
但一定连着某个 relay，于是我们在 relay 上做了一层应用层转发：

```
调用方 ──AETHER_REQ──▶ relay ──AETHER_FWD──▶ 目标节点
调用方 ◀──AETHER_RES── relay ◀──AETHER_RES── 目标节点
```

相比 UDP 打洞（DCUtR），它不关心 NAT 类型，对称 NAT 一样能穿；复用已有的 WebSocket
长连接，不需要额外端口。代价是全部流量过 relay，所以路由打分对 `relay-only` 节点
扣分（-18），能直连时仍优先直连。

协议与 Nostr 的 NIP-01 消息共存于同一条连接，靠 `AETHER_` 前缀区分：

| 消息 | 方向 | 作用 |
|---|---|---|
| `AETHER_HELLO` | 客户端 → relay | 登记身份，签名：`aether-hello:{pubkey}:{ts}` |
| `AETHER_REQ` | 调用方 → relay | 请求转发，签名覆盖 req_id/target/method/path/body |
| `AETHER_FWD` | relay → 目标 | 转发给目标节点 |
| `AETHER_RES` | 目标 → relay → 调用方 | 分块回传，`done=true` 结束 |

三个可靠性设计：目标刚上线时可能还没完成注册，`404 不在线` 会按短退避重试 3 次；
身份注册每 20 秒重新发一次，不依赖连接建立瞬间的时序；非流式请求整体回传，避免
调用方拿到被拆碎的 SSE 文本。

**部署形态**：任选一台机器（或一台公网 VPS）跑 relay，其余节点都把
`ws://<relay地址>/relay` 加进「网络 → Relay」。同一局域网直接可达时优先直连，
不走隧道。

三种方式，按成本从低到高：

1. **同一局域网**：直接可达。`A` 开启广播后，`B` 在「网络 → Relay」里添加
   `ws://<A的局域网IP>:8737/relay` 即可互相发现
2. **有公网 VPS**：任一台机器把 8737 端口映射出去，其余节点连它的 `/relay`
3. **完全隔离**：任选一台作为汇聚点，其余节点连它的 `/relay`（内置 relay 不需要公网）

对称 NAT 两侧直连需要 libp2p DCUtR 这类打洞，尚未实现——当前版本靠「至少一个汇聚节点」
解决，这是内网部署最常见也最稳的形态。

## 计量与结算

每次成功请求都进本地账本（SQLite，`data/ledger.db`），双视角记录：

- `role=consume` — 我调用了别人，形成对某个 peer 的**应付**
- `role=serve` — 别人调用了我，形成对某个 peer 的**应收**

金额按 `in_tok/1e6 × price_in + out_tok/1e6 × price_out` 折算，价格取广告里公布的报价。

**流式计量的坑**：llama.cpp 在 `stream=true` 时默认不返回 usage，代理层会自动注入
`stream_options.include_usage`，再从 SSE 末块提取。拿不到 usage 时记录标 `estimated=true`，
不把估算值混进精确结算。

**对账**：跨节点的同一次请求，双方各记一条，共用同一个 `req_id`（由调用方生成、
经 `AETHER_REQ` 的 biz_id 字段传到目标端）。`/api/ledger/reconcile` 按它比对两侧 token 数，
能发现记录不一致。这是「双方自述 + 交叉核对」，不是输出验证——无法证明对方真的跑了
声明的模型，那需要抽样复算或 TEE。

**结算**：`/api/ledger/settle` 把未结算记录聚合成批次，叶子 =
`sha256(req_id|peer|model|in|out|amount)`，算排序无关的 Merkle root，导出
`data/settlements/<batch>.json` 凭证，含全部叶子与明细，可用 `/api/ledger/verify` 离线复算。

**收据**：非流式的跨节点请求会带 `X-Aether-Receipt`（提供方对 token 数与价格的签名自述）。
流式拿不到——响应头在流开始时就已发出，那时 token 数还不知道，这类记录 `receipt=null`。

**信用**：`peer_credit` 表按 peer 累计应收/应付，GUI 里能看到净额。默认不做额度限制，
避免刚上线就把网络锁死；结构已就位，后续可接「净额为负超阈值则暂停服务」。
## 抽样复算

账本对账只能发现「两边记录不一致」，发现不了「声称跑 70B 实际跑 7B」。抽样复算补这一层：

调用方在跨节点非流式请求完成后，按 `audit_rate` 概率触发后台复算——把同一请求
（`temperature=0`、固定 `seed`、非流式）发给**网络内另一个提供同模型的节点**，比对输出。

判定逻辑：

| 情况 | 判定 |
|---|---|
| 文本逐字相同且 token 数相同 | pass |
| 文本相同但 token 数不同 | suspect（分词器或模型版本不一致） |
| 文本不同、token 数相同、相似度 ≥ 阈值 | pass（后端浮点差异） |
| 其余 | suspect |

阈值默认 0.85。没用更严的值是因为中文短答对字符级相似度很敏感——一个空格就能掉 0.03。

**操作入口**：GUI 账本页有「手动复算」按钮，也可直接调 `POST /api/ledger/audit/probe`，
它会向所有提供该模型的节点各跑一次并两两比对，不必等抽样命中。自动抽样把
`audit_rate` 调到 0.05 之类的值即可。

**验证者随机化**：从候选里随机挑验证者，不固定取第一个——固定选择意味着作恶者只要与
「排第一的那个」串通就能绕过审计。同时排除与目标同主机的候选，避免一台机器自己验证自己。

**同主机女巫降权**：一个 IP 后面挂多个身份是最常见的刷分形态。路由按同主机簇大小降权
（每多一个身份扣 6 分），让独立主机排在前面，但不禁用——同机多实例是合理用法。
簇大小会在路由候选的 note 里显示出来。

**已知边界**：需要网络内至少两个节点提供同一模型，否则记录为 `skipped`。同机两个实例
跑同一个 GGUF 时输出必然一致，这类「天然通过」不代表审计有效——真正能抓作弊的是
跨机器、跨量化档位的复算。

## 贡献与代币发放

两类贡献换算成统一点数：

| 贡献类型 | 计量方式 | 权重 |
|---|---|---|
| 推理服务 | 账本里 `role=serve` 的 token 当量 | 1 点 / token |
| 中继带宽 | 内置 relay 承载的字节数 | 500 点 / MB |

带宽权重远高于算力，因为算力可以闲时白嫖，而 NAT 后的节点没有别的办法获得可直达入口。
这也决定了激励应当从带宽起步——字节数不可伪造，而「推理是否真的跑了声明的模型」
连抽样复算都只能抓明显作弊。

**归属**：带宽贡献归 relay 提供者，不按「谁的流量」分摊。被中转的双方只是在消费这项
服务，付出中转成本的是 relay 本身。这一点最初写反了——按对端流量记账会让收请求的
服务方凭空多赚一份带宽点数。

**排放**：每小时一个 epoch，按天衰减 5%（`BASE_EMISSION × 0.95^天数`）。衰减基准在
首次计算时落盘成创世 epoch——不能拿绝对 epoch 当指数底数，`0.95^498515` 会立刻下溢成 0。

**产出**：每期一份 `data/emissions/<epoch>.json`，含全部分配明细与 Merkle root，
`/api/ledger/emission/verify` 可离线复算。链上只需要存这个 root。

**尚未解决**：多 relay 的带宽清单如何聚合、谁能代表全网发放、以及「relay 自报字节数」
如何防伪。这三件事都需要治理层，本轮只做了单 relay 的本地计量。

## 链上结算载荷

账本与发放清单的 Merkle root 已经有了，这一层负责把它们变成**可直接广播的 EVM 交易**。

全部自实现，不引入 web3 / eth-account / solc：

| 模块 | 内容 | 验证方式 |
|---|---|---|
| `evm/keccak.py` | 原始 Keccak-256（不是 SHA-3），Keccak-f[1600] 24 轮 | 空串、`abc`、ERC-20 topic 向量 |
| `evm/rlp.py` | RLP 编码 | 以太坊 wiki 的 9 组向量 |
| `evm/ecdsa.py` | RFC6979 确定性 k、low-s 规范化、地址推导、签名恢复 | EIP-155 官方向量 |
| `evm/tx.py` | EIP-155 交易签名与解码 | 官方向量逐字节比对 |
| `evm/abi.py` | 函数选择器与静态参数编码 | selector 与 calldata 字段核对 |

**节点身份就是 EVM 账户**——同一条 secp256k1 曲线，地址是公钥的 keccak 后缀。私钥不离开
`Identity` 对象，签名通过 `identity.sign_evm_transaction()` 完成。

合约在 [contracts/AetherSettlement.sol](aether-node/contracts/AetherSettlement.sol)，
只做两件事：锚定某期的 root（同 epoch 只允许提交一次）、以及用证明验证单条账目。

**一个必须对齐的细节**：Merkle 内部节点用 `sha256(abi.encodePacked(a, b))`，每对先按
字典序排序。Python 侧原来的实现有两个问题——先是对十六进制字符串哈希（合约对的是原始字节），
后是构建时按数组位置配对、验证时按字典序配对，两套规则导致同一片叶子的证明时真时假。
现在构建与验证共用 `_pair()`，1/2/3/5/7/9 片叶子都验证通过。

**交易类型**：默认出 legacy（EIP-155），传 `tx_type: 2` 出 EIP-1559。后者结构是
`0x02 || rlp([chainId, nonce, maxPriorityFee, maxFee, gasLimit, to, value, data, accessList, yParity, r, s])`，
签名哈希用前九个字段。实测同一条 calldata 两种格式都能签出，解码往返一致、恢复地址正确。

**使用流程**：

```powershell
# 1. 结算账目，得到 root
Invoke-RestMethod -Uri "http://127.0.0.1:8737/api/ledger/settle" -Method Post

# 2. 生成签名交易（不广播）
Invoke-RestMethod -Uri "http://127.0.0.1:8737/api/ledger/onchain/sign" -Method Post `
  -ContentType "application/json" -Body '{"to":"0x合约地址","root":"0x...","epoch":498515,"amount":1000000000,"chain_id":31337,"nonce":0}'

# 3. 显式提供 rpc_url 才会广播
Invoke-RestMethod -Uri "http://127.0.0.1:8737/api/ledger/onchain/broadcast" -Method Post `
  -ContentType "application/json" -Body '{"rpc_url":"http://127.0.0.1:8545","raw":"0x..."}'
```

**未做**：合约没有发放代币的逻辑，也不含代币发行。它只证明「这批账目在某时刻被提交过」。
真实的资金转移需要另接 ERC-20 或支付通道，那属于经济机制设计，不是这层的职责。

## 代币发放与领取

`contracts/AetherToken.sol` 是最小 ERC-20（自包含，不引 OpenZeppelin，带硬上限）。
`contracts/AetherDistributor.sol` 是发放器，把「发放」和「领取」拆开：

```
每期：owner 调 setRoot(epoch, root, total)  ← 链上只存一个 32 字节 root
节点：调 claim(epoch, amount, proof)         ← 自己带证明来领，合约 mint 给调用者
```

链上开销与节点数量无关——一千个节点和十个节点都只多存一个 root。

**叶子定义**：`sha256(address(20 字节) || amount(uint256 大端))`。刻意不含点数和 epoch：
点数是链下记账口径，浮点在链上没有表示，一旦进叶子就无法用合约重算。

**EVM 地址必须由节点自己公布**。节点 ID 是 x-only 公钥（32 字节，缺 y），从中推不出唯一的
EVM 地址（y 有奇偶两种可能，`_lift_x` 只能给出偶数的那个）。所以服务广告里带 `evm` 字段，
由节点用自己的私钥算出后广播。缺地址的节点在发放时会被记为 `skipped` 并写明原因，不会静默丢账。

**API**：

| 端点 | 作用 |
|---|---|
| `GET /api/ledger/payout/current` | 本期叶子、证明、setRoot calldata |
| `POST /api/ledger/payout/claim` | 某个节点的 claim calldata |
| `POST /api/ledger/payout/verify` | 本地校验证明（等价于合约 verifyLeaf） |
| `GET /api/ledger/payout/contracts` | 三个合约文件的位置 |

**未做**：合约没有经过 solc 编译，也没有在真实链上部署或跑过。Solidity 代码是照标准写的，
但「能编译、能部署、gas 够」这三件事只有真编译过才算数。Python 侧的 calldata 编码、
叶子定义、证明路径都已逐字段验证。

## 带宽对账的边界

隧道每笔请求都会记录双向字节：调用方统计实际收到的响应字节，提供方在
AETHER_RES 的最后一块附上它统计的字节数，两者偏差超过 5% 或 64 字节就记一次
yte_mismatch。实测一次完整隧道会话收到 9805 字节、零偏差。

**但这不能证明 relay 没有虚报承载量**。relay 声称「我转了 10TB」时，单个节点只能看到
自己经过的那部分；要揭穿虚报需要把全网各 client 的观测求和后与 relay 自报对比，
那属于治理层，本轮没做。这里能做到的是证明**单笔传输过程没有被裁剪或篡改**。

把这条写清楚，是为了避免把「有字节统计」误读成「解决了带宽造假」。

## 架构

```
L1 服务    llama-server.exe（OpenAI 兼容）
L2 发现    Nostr relay（内置 + 公共），签名服务广告，kind 30333
L2.5 穿透  中继隧道（AETHER_* 消息），relay-only 节点经汇聚 relay 被调用
L3 路由    Router：本机优先 → 网络打分（可达性/时效/价格）
L4 接口    /v1/models, /v1/chat/completions（含 SSE 流式）
L5 计量    账本 + 收据 + 对账 + 结算批次（Merkle root）
L6 应用    FastAPI + SSE 事件流 + pywebview / Edge --app 窗口
```

双层签名：外层是 Nostr 事件签名（Schnorr over event id），内层是广告正文的规范化 JSON 签名。
即使广告被任意 relay 转发，接收方仍可独立验真。

secp256k1 / BIP340 用纯 Python 实现（`aether/secp256k1.py`），因为标准库与该曲线无关、
而签名频率极低（广告每 2 分钟一次）。已用 BIP340 官方向量校验。

## 目录

```
aether/
  config.py        设置与路径
  identity.py      节点身份（secp256k1）
  secp256k1.py     BIP340 Schnorr
  hub.py           HF REST 客户端（搜索/文件树/断点续传）
  gguf.py          GGUF 元数据解析（层数/上下文/头数）
  hardware.py      GPU/RAM 探测与参数推荐
  library.py       本地模型库
  downloads.py     下载任务管理
  llama_server.py  llama-server 子进程与能力探测
  p2p/
    ad.py          服务广告 schema 与双层签名
    nostr.py       NIP-01 客户端（多 relay、自动重连）
    relay_server.py 内置 relay（内存、可替换事件）+ 隧道转发 + 带宽计量
    tunnel.py      应用层中继隧道（AETHER_REQ/FWD/RES）
    discovery.py   peers 表与 TTL
    router.py      统一目录与打分
    upstream.py    远端转发（含流式与隧道）
    service.py     编排：连接、发布、刷新、隧道注册
  ledger.py        按 token 计量的账本（SQLite，双视角）
  metering.py      usage 提取（完整响应 / SSE 尾部）
  receipt.py       服务收据的签发与验证
  settlement.py    结算批次、Merkle 根与证明
  audit.py         抽样复算与判定
  emission.py      贡献度换算与代币发放计划
  payout.py        发放清单 → 链上叶子、证明与 calldata
  evm/             自实现的以太坊工具链
    keccak.py      原始 Keccak-256（不是 SHA-3）
    rlp.py         RLP 编码
    ecdsa.py       RFC6979 + low-s + 地址推导 + 签名恢复
    tx.py          EIP-155 交易签名与解码
    abi.py         函数选择器与 calldata 编码
  api/             FastAPI 路由与 SSE
  gui/             前端（原生 JS）
contracts/
  AetherSettlement.sol  锚定结算根 + 叶子验证
  AetherToken.sol       最小 ERC-20
  AetherDistributor.sol 按根发放、节点自领
tests/
  test_secp256k1.py  BIP340 向量与往返
  test_p2p_ad.py     广告签名/验签/篡改/发现表
  test_evm.py        Keccak/RLP/EIP-155/ABI/Merkle 全量向量
  test_payout.py     叶子定义、证明、calldata 编码
  e2e_*.ps1          七个端到端场景
```

## 已验证（本机实测，2026-11-14）

**密码学与协议层**

- `tests/test_secp256k1.py`：BIP340 官方向量（vector0 公钥）、2G 坐标、签名往返、篡改消息/签名拒绝、确定性 nonce，7 项全过
- `tests/test_p2p_ad.py`：广告双层签名、事件验签、篡改检测、TTL 过期、发现表去重与自广告过滤，22 项全过
- 24 个模块可干净导入（含项目自带 venv）

**单机全链路**

- 从 HF 下载 `Qwen2.5-1.5B-Instruct-GGUF:Q2_K` 718MB，速度稳定爬升到 2.7 MB/s
- GGUF 元数据解析：`layers=28 ctxTrain=32768`，据此推荐 `ngl=-1`（全量卸载）与 `ctx=8192`
- llama.cpp 运行时自动下载解压，实测后端 `CUDA0: GeForce RTX 4070 Ti (12281 MiB)`
- 加载到 ready 用 5 秒；对话 34+28 tokens 耗时 **0.2 秒**
- `/v1/models`、`/v1/chat/completions`（含 SSE 流式）、token 计量全部正常

**跨节点路由（核心能力）**

- 节点 A（18800）加载模型并广播，节点 B（18801）无任何本地模型
- B 通过 A 的内置 relay 发现：`reach=lan endpoint=http://192.168.1.6:18800`
- B 的 `/v1/models` 列出 `Qwen2.5-1.5B-Instruct-GGUF:Q2_K[aether-remote]`
- B 发起对话 → 响应头 `X-Aether-Route: remote`、`X-Aether-Node: MS-NQDAKZSMKTFU`、`X-Aether-Score: 61.92`，0.5 秒返回
- 流式经远端转发同样正常（45 个 SSE 片段）
- 路由候选与打分可见：`kind=remote score=61.9 note=lan / 广告龄 3s`

**中继隧道（无直达端点也能调用）**

- A 只监听 `127.0.0.1`（模拟 NAT 后不可达），广告里 `reachability=relay-only` 且不公布端点
- A 与 B 都接入同一个汇聚 relay，B 侧目录显示 `来自 …（经 relay 隧道）`
- B 的路由候选为 `kind=relay score=11.91`
- 非流式对话经隧道返回 200：`X-Aether-Route: relay`，1.5 秒，37+15 tokens
- 流式对话经隧道拿到 34 个 SSE 片段，拼接出完整回答
- relay 侧统计 `forwarded_in=2 served=2`，目标侧 `last_status=200`

**计量与对账**

- 非流式请求：账本记 `32+7`，与 llama-server 返回的 `usage` 完全一致
- 流式请求：注入 `stream_options.include_usage` 后，账本记 `33+30`，与上游 usage 一致
- 跨节点（A 提供、B 经隧道调用）：双方各记一条，`req_id` 相同、`35+8` 相同、金额 `7.65e-06` 相同
- 信用净额对称：B 应付 `-1e-05`，A 应收 `+1e-05`
- 结算批次生成 Merkle root，`/api/ledger/verify` 独立复算通过、无异常叶子

**抽样复算**

- 两个节点各跑同一 GGUF，对同一请求输出均为 `等于2。`、usage 均 `42+4`，相似度 1.0 → pass
- 判定逻辑单元测试：逐字相同 pass；token 数不同而文本相同 → suspect；
  内容完全不同 → suspect；只有一侧为空 → suspect
- 自动抽样：`audit_rate=1.0` 后一次普通请求在 10 秒内触发复算，
  `target` 是被调用方、`verifier` 是第二个提供同模型的节点，判定 pass

**贡献与发放**

- 三次隧道请求后：A 的发放清单只含自己的 191 点服务贡献（`bytes=0`），
  B 的清单只含自己的 2477 字节中继贡献（1.181 点）——归属互不污染
- 衰减曲线：day0 `10000` → day1 `9500` → day7 `6983` → day30 `2146`
- 分配与校验：nodeA 6MB 中继对应 3000 点 / share 0.857 / 8571.43；
  nodeB 1MB 对应 500 点 / 1428.57；合计等于本期排放，Merkle 校验通过

**非回环网络路径**

- 	ests/e2e_lan.ps1 走真实网卡 IP（192.168.x.x）而不是 127.0.0.1
- 回环无 key -> 200；局域网 IP 无 key -> 401；带 X-Api-Key / Bearer -> 200
- relay：127.0.0.1 接受，局域网 IP 无 token 被拒（InvalidStatus），带 token 接受
- 配置 peer_keys 后 B 经局域网直连 A：HTTP 200、路由 remote、回答正常

**EVM 载荷**

- `tests/test_evm.py` 37 项全过：Keccak 空串/`abc`/ERC-20 topic、RLP 九组向量、
  EIP-155 官方交易 raw 逐字节一致、v=37、地址恢复回签名者、low-s 规范化、
  ABI selector 与三个参数字、Merkle 证明 9 片叶子全通过、篡改叶子被拒、
  完整链路（批次 root → calldata → 签名 → 解码）逐字段一致
- API 实测：节点 `0x9f8e0946…27a3` 推出 EVM 地址，calldata 100 字节，
  签名交易 v=62710 对应 chain_id 31337，`from` 与节点地址一致

**代币发放**

- `tests/test_payout.py` 29 项全过：叶子与合约定义逐字节一致、改金额或改地址都让证明失效、
  跨 claim 借用证明被拒、setRoot 三个参数字正确、claim 的数组 offset=0x60、
  proof 元素逐个核对、空清单与单叶子的边界
- API 实测：一次本地请求产生 1 条 claim（`10000e18`），单叶子 proof 为空数组、verify=True，
  claim calldata 133 字节、setRoot calldata 100 字节

**安全基线**

- `tests/test_security.py` 19 项：默认绑回环、共享模式自动生成 key、
  老配置 `0.0.0.0` 迁移后仍对外但已带 key、无 key/错 key 被拒、正确 key 放行、
  Bearer 与 X-Api-Key 两种头都认、`/v1` 同受保护、静态 GUI 不设限、关闭共享后恢复
- `tests/test_guard.py` 15 项：正常请求被转发且 biz_id 携带、同 req_id 重放被拒、
  速率配额耗尽被拒、签名错误被拒、未注册身份被拒、并发闸门准入与释放

复现脚本：`tests/test_secp256k1.py`、`tests/test_p2p_ad.py`、`tests/test_evm.py`、
`tests/test_payout.py`、`tests/e2e_verify.ps1`、`tests/e2e_load.ps1`、`tests/e2e_route.ps1`、
`tests/e2e_tunnel.ps1`、`tests/e2e_ledger.ps1`、`tests/e2e_ledger_cross.ps1`、
`tests/e2e_audit.ps1`、`tests/e2e_emission.ps1`、`tests/e2e_external.ps1`、
`tests/e2e_fault.ps1`、`tests/e2e_lan.ps1`、`tests/e2e_reconnect.ps1`

**隧道重复转发去重**

调用方把 `AETHER_REQ` 广播给所有 relay，若两个 relay 都认识目标节点，同一请求会被转发两遍。
目标端现在按 `req_id` 去重：重复的计入 `duplicate_forward` 但不执行、不回复——否则会白白
重复烧算力。记录保留 5 分钟并定期清理，不会无界增长。`tests/test_dedup.py` 11 项覆盖
去重、误判（不同 req_id 不应被当重复）与过期清理。

**回程中断与部分拒绝**（`tests/test_returnpath.py`）——只做前半程的异常路径：

| 场景 | 结果 |
|---|---|
| 请求发出但回执永不返回 | 按 `tunnel_timeout` 超时（可配置），中间表全清 |
| 所有 relay 都回绝 | 5 秒内失败，远小于 30 秒超时，错误写明「所有 relay 都拒绝转发」 |
| 两个 relay 一个回绝、一个稍后成功 | 正常成功，不会被单个拒绝带偏 |

超时从配置读而不是硬编码——硬编码的 180 秒在测试里根本没法验证超时路径。

**长稳曲线**（`tests/e2e_soak.ps1`）——5 分钟、600 个请求，每 20 秒采样：

| 指标 | 结果 |
|---|---|
| 内存 | 60.5MB → 65.4MB，增长 1.3MB（2.1%） |
| 错误 | 0 / 600 |
| inflight | 全程 0 |
| 账本 | 600 条，与请求数一致 |

2.1% 的爬升是 Python 对象池与 SQLite 缓存的正常预热，不是泄漏——真正的泄漏会表现为
持续单调增长且不回落，而这里后半段已经趋平。

**采样踩过的坑**：第一次跑出来内存恒定 4.0MB，看着像「零增长」，其实 `Start-Process`
返回的 PID 是 launcher（4MB），真正跑服务的是它的子进程（60MB）。改成按命令行匹配取
最大进程后才是真实数字。**一个错误的采样口径能让最关键的指标看起来完美**。

## 静态检查

```powershell
.venv\Scripts\python.exe -m ruff check aether
.venv\Scripts\python.exe -m mypy aether --ignore-missing-imports
```

两者现在都是零告警，CI 里也跑同样的命令。

这不是形式主义——这一轮和上一轮靠它们抓到的都是**运行测试覆盖不到**的东西：

| 工具 | 抓到的问题 |
|---|---|
| ruff | `routes_system.py` 有 7 处 `raise HTTPException` 却没导入它，错误分支会抛 `NameError` 而不是返回 4xx |
| mypy | `library.py` 里局部变量 `m` 同时被当作 `LocalModel` 与正则 `Match`；`hardware.py` 里 `meta` 可能为 None 却直接访问 `.ok`；`auth.build()` 声明返回 `object` 导致调用方类型全丢 |

运行测试验证行为对不对，ruff 验证代码自洽，mypy 验证类型契约——三者覆盖的面完全不同。
前两轮的产出密度明显高于继续写功能测试。

## 尚未实现

- UDP 打洞（libp2p DCUtR）：当前无直达端点时走 relay 隧道，不建立点对点直连
- 链上结算：账本与批次已就绪，但 Merkle root 尚未上链，也没有 x402 / 状态通道
- 中继带宽与算力的代币激励
- 更强的输出验证：抽样复算只能抓明显作弊（换小模型、换量化档），
  TEE attestation 才能证明「确实在声明的硬件上跑了声明的模型」

这三项是路线图的下一阶段。账本层的数据结构已为它们留好接口（`batch.merkle_root`、
`receipt`、`audits` 表）。
