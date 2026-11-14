# AetherNode

把本地 AI 服务打包成接口、广播到 P2P 网络、并对请求做路由的桌面程序。

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

四个视图：

- **模型库** — 搜索 HF、选量化、看下载进度、管理本地模型
- **运行时** — 当前模型参数（GPU 层数、上下文）、llama-server 实时日志
- **网络** — 节点身份、relay 连接、发现的 peers、统一模型目录、广播开关
- **对话** — 直接测试，并显示本次请求实际路由到了哪个节点

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

**已知边界**：需要网络内至少两个节点提供同一模型，否则记为 `skipped`。同机两个实例
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
`tests/e2e_audit.ps1`、`tests/e2e_emission.ps1`

## 尚未实现

- UDP 打洞（libp2p DCUtR）：当前无直达端点时走 relay 隧道，不建立点对点直连
- 链上结算：账本与批次已就绪，但 Merkle root 尚未上链，也没有 x402 / 状态通道
- 中继带宽与算力的代币激励
- 更强的输出验证：抽样复算只能抓明显作弊（换小模型、换量化档），
  TEE attestation 才能证明「确实在声明的硬件上跑了声明的模型」

这三项是路线图的下一阶段。账本层的数据结构已为它们留好接口（`batch.merkle_root`、
`receipt`、`audits` 表）。
