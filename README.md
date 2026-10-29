# 同一模型的 Cross-Harness Benchmark

用**同一个模型**，比较不同 coding-agent harness 在同一批任务上的解题能力、延迟、模型请求数与本地资源消耗。保留每种 harness 原生的提示、工具和 Agent loop，通过本地网关固定模型，并使用数据集评分器评分。

本仓库只分发执行、评分、统计和绘图代码及软件/数据集版本配置。运行产生的数据、任务文件、图表和报告均保存在本地 `runs/`，不随仓库提供。

支持的数据集：SWE-Bench Verified 1.0、Terminal-Bench **2.0**、AppWorld 0.1.3.post1。支持的 harness 配置名称：`codex`、`claude`、`dsh`、`opencode`、`pi`、`kimi`。

## 1. 环境与安装

运行环境为 Linux x86_64、Docker Engine + Compose v2、cgroup v2。宿主需要 Python 3.12、Git、curl、`dpkg-deb` 和访问软件源/数据集源的网络。Docker Desktop、Windows/macOS 原生运行、远程 Docker 和 ARM 尚不支持本地 cgroup 采样。执行账户需要 Docker 权限。

### 1.1 从空机器准备系统环境

以下以 **Ubuntu 24.04 x86_64** 为例。已有对应组件可跳过安装；其它 Linux 发行版请使用相应软件源，Python 版本仍需为 3.12。

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates dpkg tar xz-utils unzip \
  python3.12 python3.12-venv python3-pip

# 若尚未安装 Docker，Ubuntu 24.04 软件源可安装以下包
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

加入 `docker` 组后，退出登录再重新登录，使组权限生效。也可按 [Docker 官方指南](https://docs.docker.com/engine/install/ubuntu/) 安装 Docker Engine 和 Compose 插件；已有 Docker 时不必混装两套软件包。检查当前用户的访问权限和 cgroup：

```bash
python3.12 --version              # 应为 Python 3.12.x；系统的 python3 可能是其它版本
python3.12 -m venv --help          # 确认已安装 venv 模块
docker version                   # Client 和 Server 均应可访问
docker compose version           # 应为 v2
docker info --format '{{.CgroupVersion}}'  # 应为 2
stat -fc %T /sys/fs/cgroup        # 应为 cgroup2fs
```

### 1.2 推荐：自动创建隔离环境并安装

安装器只使用 Python 标准库启动，**不要求事先存在任何 `.venv-*` 环境**。在仓库根执行：

```bash
git clone https://github.com/24Kgule/Cross_Harness_Benchmark.git
cd Cross_Harness_Benchmark
python3.12 -m harness_benchmark.install --appworld
```

`--appworld` 为可选项；只运行 SWE/TB 时可以省略。安装器先下载固定运行时和六种 CLI，再用固定的 **CPython 3.12.15** 自动创建 `.venv-harbor`，按根目录 [`requirements.txt`](requirements.txt) 安装主程序依赖；指定 `--appworld` 时，还会创建 `.venv-appworld` 并按 [`requirements-appworld.txt`](requirements-appworld.txt) 安装独立依赖。两个入口分别引用 `configs/` 中的完整版本锁文件，避免维护两份版本清单。

安装器把 Node、Python、uv 和六种 CLI 安装到本仓库的私有路径，不写入用户全局 harness 配置：

| 组件 | 固定版本 / 路径 |
|---|---|
| Codex | 0.163.0 |
| Claude Code | 2.1.310 |
| dsh | 0.2.0-rc.2 |
| OpenCode | 1.19.4 |
| Pi | 1.4.0 |
| Kimi Code（npm CLI） | 2.3.1 |
| Harbor | 0.24.0，`.venv-harbor/` |
| AppWorld | 0.1.3.post1，`.venv-appworld/` |
| Node / Python | 24.9.0 / 3.12.15；TB 评分另备 3.13.5 |

安装以 `vendor/*/package-lock.json`、`configs/*.requirements.lock.txt` 和二进制 SHA256 为准。需要改变版本时，更新相应锁文件，并使用新的输出目录。不要以不同版本的程序续跑同一批实验。`--skip-binary-hash-check` 仅用于主动采用其它运行时的情况，不能据此声称二进制一致。

### 1.3 Python 环境分工与手动安装

| 环境 | 依赖入口 | 用途 |
|---|---|---|
| `.venv-harbor`，Python 3.12.15 | `requirements.txt` → `configs/harbor.requirements.lock.txt` | 主执行器、Harbor、API 网关、资源采样、统计绘图和本地测试 |
| `.venv-appworld`，Python 3.12.15 | `requirements-appworld.txt` → `configs/appworld.requirements.lock.txt` | 可选的 AppWorld 世界进程、官方 CLI 和评分 |
| `runs/<experiment>/dependencies/swe-parser` | `configs/swe-parser.requirements.lock.txt` | SWE 官方评分解析；由 `prepare` 自动创建，无需手工安装 |

**不要把 AppWorld 与主程序依赖装进同一个虚拟环境**：AppWorld 需要 Pydantic 1，Harbor 需要 Pydantic 2。Python requirements 只安装 Python 包；完整实验还需运行上面的安装器，以准备 CLI、Node、固定 Python 和评分工具。AppWorld 数据的解密与下载见第 4 节。

如果仅需检查代码或对已有 CSV 绘图，可用系统 Python 3.12 手动创建主环境，不需要先下载 CLI：

```bash
python3.12 -m venv .venv-harbor
.venv-harbor/bin/python -m pip install -r requirements.txt
.venv-harbor/bin/python -m pip check
```

完整实验以自动安装的固定运行时为准。若需要在运行时已下载后手动重建 Python 环境，在退出已激活的虚拟环境后执行以下命令；重建前请自行移走已有的同名环境：

```bash
.runtime/python/cpython-3.12.15-linux-x86_64-gnu/bin/python3.12 -m venv .venv-harbor
.venv-harbor/bin/python -m pip install -r requirements.txt
.venv-harbor/bin/python -m pip check

# 仅当实验包含 AppWorld 时需要
.runtime/python/cpython-3.12.15-linux-x86_64-gnu/bin/python3.12 -m venv .venv-appworld
.venv-appworld/bin/python -m pip install -r requirements-appworld.txt
.venv-appworld/bin/python -m pip check
```

检查解释器与关键依赖：

```bash
.venv-harbor/bin/python --version
.venv-harbor/bin/python -c 'from importlib.metadata import version; print({p: version(p) for p in ("harbor", "aiohttp", "psutil", "numpy", "matplotlib", "pydantic")})'
# 仅在安装 AppWorld 后执行
.venv-appworld/bin/python -c 'from importlib.metadata import version; print({p: version(p) for p in ("appworld", "pydantic")})'
```

自动安装使用 `uv` 创建环境并同步依赖，不保证环境中带有 `pip` 命令。需要重新同步或检查自动安装的环境时，使用仓库自带的 `uv`：

```bash
.runtime/bin/uv095/uv pip sync --python .venv-harbor/bin/python requirements.txt
.runtime/bin/uv095/uv pip check --python .venv-harbor/bin/python
# 仅在安装 AppWorld 后执行
.runtime/bin/uv095/uv pip sync --python .venv-appworld/bin/python requirements-appworld.txt
.runtime/bin/uv095/uv pip check --python .venv-appworld/bin/python
```

后续示例直接指定 `.venv-harbor/bin/python`，因此**无需激活环境**。若更喜欢激活方式，可执行 `source .venv-harbor/bin/activate`，之后使用 `python -m ...`，完成后执行 `deactivate`；不要同时激活两套环境。`run` 会按配置启动独立的 AppWorld Python，主命令仍在 Harbor 环境中运行。

全量 SWE 镜像可能需要数百 GB 以上磁盘；任务的下载、镜像和评分准备时间与模型执行时间分开。先使用少量样本检查本机容量，再扩大规模。

## 2. 配置一个模型

```bash
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写：

```dotenv
model_name=YOUR_EXACT_MODEL_ID
url=https://YOUR_RELAY_HOST/v1
NEW_API_KEY=YOUR_API_KEY
```

模型、地址和密钥仅从这三个字段读取。实际 API key 留在宿主网关；任务容器只收到逐任务代理 token，不挂载 `.env`、网关 master 或 AppWorld 宿主数据。不要上传 `.env`。

再修改 [`configs/api.active.json`](configs/api.active.json)：

- `accepted_response_models`：填写服务端实际可能返回的**精确模型标识**。请求仍只能使用 `.env` 中的一个模型；该列表只允许同一模型的回显别名，不用于选择多个模型。标识异常会停止后续请求。
- `requested_context_tokens`：提供给各客户端的上下文配置。
- `requested_max_output_tokens`：逐请求统一输出上限。网关保留客户端较小的原始上限，只截低超限值。

默认 API 配置提供 DeepSeek V4.1 Flash 的标识示例。更换模型时须同步修改接受的回显和容量参数；短连通测试不能证明完整上下文容量。

服务端需提供三种原生接口及对应工具调用/推理支持：Codex 用 Responses，Claude/dsh/Kimi 用 Messages，OpenCode/Pi 用 Chat Completions。网关按原协议转发。仅提供 Chat Completions 的服务不能直接运行全部六组；请在实验配置中选择服务端支持的 harness。当前启动器以支持 high 推理档位的模型为目标，换用其它模型需先核查协议和推理参数。

配置说明见 [`experiment_api/README.md`](experiment_api/README.md)。本地检查不会请求模型：

```bash
.venv-harbor/bin/python -m harness_benchmark doctor --config configs/quickstart.json
```

需要实际验证 API 时可**单独执行**下列探测。它会产生少量 token 用量；不会计入 benchmark：

```bash
.venv-harbor/bin/python -m experiment_api.probe --output runs/connectivity-check
```

## 3. 用 config 选择实验

编辑 [`configs/benchmark.json`](configs/benchmark.json)，或复制 [`configs/quickstart.json`](configs/quickstart.json)。例如只测试 dsh、Pi 的 10 个 SWE 和 40 个 TB 任务：

```json
{
  "schema_version": 1,
  "output_dir": "runs/my-experiment",
  "api_config": "configs/api.active.json",
  "harnesses": ["dsh", "pi"],
  "datasets": [
    {"name": "swebench-verified", "sample_size": 10, "seed": 42},
    {"name": "terminal-bench", "sample_size": 40, "seed": 42},
    {"name": "appworld", "enabled": false}
  ],
  "execution": {"workers": 2, "max_requests": 50000}
}
```

| 字段 | 用法 |
|---|---|
| `harnesses` | 非空、不重复的 harness 名称数组；可选任意子集 |
| `datasets` | 要参与实验的数据集；可省略不需要的项，或设 `enabled: false` |
| `sample_size` | 正整数，或字符串 `"all"`；每个数据集独立配置 |
| `seed` | 抽样随机种子，默认 20261029；同一数据集的所有 harness 共用样本 |
| `task_ids` | 手选任务名称数组；与 `sample_size` 二选一，例如 `["fix-git"]` |
| AppWorld `split` | 默认 `test_normal`；也支持 `train`、`dev`、`test_challenge` |
| `output_dir` | 本次实验的独立输出目录；相对路径以仓库根目录为基准 |
| `api_config` | 公共 API 配置路径；真实凭据仍只读取仓库根 `.env` |

完整候选目录位于 `configs/datasets/`：SWE 500 题、TB2 89 题、AppWorld test_normal 168 题。抽样先按名称排序，再以 `seed:dataset:split` 为字符串种子打乱并取前缀。相同配置得到相同样本；改变 harness 子集不改变样本。AppWorld 其它 split 的候选名称从已下载的 split 文件读取，因此需先下载其数据再 `plan`。

`sample_size: "all"` 选择相应数据集/划分全量。默认主配置为 25/40/50 题和六种 harness，可自行修改；每题每个 harness 执行一次，不把六个 harness 的观测当成六个独立任务样本。

执行字段及默认值：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `workers` | 2 | 同时执行的任务容器数；包括其评分阶段 |
| `minimum_cpus` | 2 | CPU 下限；实际取本值与官方任务声明的较大值 |
| `minimum_memory_mb` | 6144 | 内存下限；同样保留更高的官方需求 |
| `max_requests` | 50000 | 本实验跨续跑累计的模型上游请求上限，含辅助请求和失败请求 |
| `agent_network` | `task-default` | 允许任务网络；`isolated` 在 Agent 阶段断开容器外网，评分前恢复 |
| `image_mirror` | `""` | 可选拉镜像前缀；保留原镜像引用并锁定实际 image ID |
| `pypi_index` | `https://pypi.org/simple` | 为六组统一指定 Python 软件源 |
| `hf_endpoint` | `null` | 可选任务制品传输端点，作用于整个任务容器 |
| `appworld_root` | `datasets/appworld` | AppWorld 宿主数据根目录 |
| `appworld_python` | `.venv-appworld/bin/python` | 已安装并解密 AppWorld 的 Python |
| `appworld_image` | `python:3.12-slim` | AppWorld Shell 容器镜像，准备时固定 ID |
| `appworld_timeout_sec` | 1800 | AppWorld Agent 时限 |
| `appworld_max_interactions` | 200 | AppWorld 持久世界交互上限 |
| `appworld_code_timeout_sec` | 60 | 单次 AppWorld Python 执行时限 |

SWE/TB 保留官方解题和评分时限。AppWorld 宿主 worker 为每题独立进程；同一世界内部的 execute 请求串行处理。`isolated` 可能令需要下载依赖/数据的任务失败，应在比较前固定这个选择。真实服务费率未配置，金额输出为空；请按记录到的 usage 自行核账。

## 4. 拉取数据、运行、生成图表

从仓库根运行，以下命令始终使用同一份 config：

```bash
# 只预览样本规模，不读取密钥、不请求模型
python3.12 -m harness_benchmark plan --config configs/quickstart.json

# 可选：先单独下载已选 SWE/TB 题目，不拉镜像
python3.12 -m harness_benchmark prepare --config configs/quickstart.json --assets-only

# 下载题目、拉镜像、准备评分依赖；不调用模型
.venv-harbor/bin/python -m harness_benchmark prepare --config configs/quickstart.json

.venv-harbor/bin/python -m harness_benchmark doctor --config configs/quickstart.json

# 此步开始模型请求和计费
.venv-harbor/bin/python -m harness_benchmark run --config configs/quickstart.json

# 本地统计和画图，无模型请求
.venv-harbor/bin/python -m harness_benchmark plot --config configs/quickstart.json
```

`prepare` 按配置中的 task ID，从固定 Git 提交下载 SWE/TB 资产，并校验 Git blob；不需要手工克隆整个数据集。镜像拉取失败时可配置 `image_mirror`。无已识别基础镜像或评分引导的任务会停止准备，需先适配，不能静默绕过官方评分。

AppWorld 采用其官方 CLI 安装与下载。启用 AppWorld 时 `prepare` 会自动执行安装解密并下载缺失数据；也可手动先做：

```bash
.venv-appworld/bin/appworld install
.venv-appworld/bin/appworld download data --root datasets/appworld
```

若自定义了 `appworld_root`，手动命令须使用同一个目录。test_normal split 名单有固定 SHA 检查，其它划分会冻结选定 task ID。遵循各数据集自己的使用条款。

执行后输出结构如下（均为自动生成，不需手写）：

```text
runs/<experiment>/
  plan.json                     # 配置、样本、执行格和计划 hash
  execution.json                # 模型、版本、代码 hash、镜像锁的执行契约
  assets/                       # 下载的原始任务
  execution_assets/             # 固定镜像引用的运行任务
  cells/<index>/record.json     # 逐格成绩、状态、时长和资源
  cells/<index>/jobs/            # Harbor 和原生 CLI 日志、官方评分证据
  requests.<segment>.jsonl       # 去内容的请求观测和 usage
  status.json                   # 当前进度
  analysis/trials.csv            # 可用于复算的逐任务表
  analysis/summary.json          # 按数据集 / harness 汇总
  analysis/REPORT.md             # 自动报告
  analysis/figures/*.png,*.svg    # 成功率、逐题矩阵、效率、资源、时间构成
```

只重画已有表格时，不需要数据集、Docker、模型密钥或 CLI 运行时：

```bash
.venv-harbor/bin/python -m harness_benchmark plot \
  --table /path/to/trials.csv --output /path/to/new-analysis
```

CSV 必需列为 `dataset,task,harness,official_score`；成绩为 `0`、`1` 或空。时间、请求数和资源列可选，名称见 `harness_benchmark/analyze.py` 的 `NUMERIC`。空值保留缺失；同一个 dataset/task/harness 不允许重复。结果图同时生成 PNG 和 SVG。

## 5. 续跑与评分约定

再次使用完全相同的配置执行 `run`，只分发尚未开始的格。配置、代码、模型公共配置或镜像锁改变时必须换 `output_dir`，不将不同条件混入同一实验。密钥轮换不改变模型公共配置 hash。

每格使用一次 Agent 执行。有效官方 0 分保留；原生 CLI 内部的请求重试仍保留并计入请求数。外层 Harbor 不自动重试 Agent。已有 `record.json` 的格，包括评分缺失，都不会重新请求模型。有 `started.json` 但无完成记录时拒绝自动续跑，需要人工确认其状态。不要删除启动记录来隐藏额外执行。

SIGINT/SIGTERM 会停止分发新格，等待在途任务自然结束。评分缺失、服务端认证/额度错误、模型回显异常或请求数到限会停止继续分发。缺失评分与已执行的真实 0 分分开保存。免费重评需使用明确保留的原环境并另存证据；本仓库不自动从文件系统快照重建运行中服务来补分。

SWE 使用原测试补丁和 swebench 4.0.3 官方日志解析/评分函数，评分前换成本地固定 parser 依赖，并以等值的五个数据字段构造解析所需对象，避免为生成无关 shell 配置再次联网。Sphinx 补充日志解析所需的 `-rA --color=no`。TB 保留原测试文件、版本约束、pytest 参数和 reward 逻辑，替换已识别的 uv 安装引导，并准备官方声明的 OS/Git 评分依赖。改动 diff 和测试文件 hash 随每次评分保存。

评分开始前会终止原生 Agent 子树、保留识别到的任务服务，再上传隐藏测试。评分依赖缓存只读挂载，不含隐藏测试或参考解答。为四个 CPU 任务提供 PyTorch CPU wheel 索引；APT 使用签名归档源。传递依赖并非全部按 wheel hash 锁定，复跑时仍应核查解析版本和软件源。

这些约束便于比较整体 harness；不会自动证明参考解答完全不可访问，也不等于隔离出了某个优化功能的因果收益。

## 6. 指标的解释

- 成功率按有有效官方评分的任务计算，同时列出有效数/计划数和共同有效 task ID。部分完成时各组有效子集可能不同，主比较应使用共同有效任务，不能只看一个百分数。
- `agent_sec` 与环境准备、评分时间分开。API 阶段按区间并集拆为首内容前、流式窗口、API 之外；重叠请求不重复计时。
- 首内容前包括网络、服务端排队和模型工作；流式窗口包括思考、文字、工具参数及传输，不能分别当成纯 prefill / decode。
- 原生 JSON 首文本是采集到的日志时间，不保证等于用户界面首个字符。API 外也包含实际工具与后台轮询，不能全称为 harness 开销。
- Agent 容器每 0.5 秒采样 cgroup v2；累计 CPU、平均/峰值 CPU、平均/采样峰值内存均记录。CPU 100% 表示一颗逻辑核。内存含任务工具、服务与页缓存。
- AppWorld 宿主世界 worker 资源另存；其采样窗口含桥接/评分工作，不与 Agent 容器 CPU 简单相加解释。共享宿主 IO/API 竞争没有被消除。
- 自动图的 95% Wilson 区间按任务计算，只作描述。AppWorld 同一家族任务相关，正式推断需另作家族聚类分析。不同数据集不合并成总成功率。
- usage 为服务端回传的观测值，不完整响应可能漏计 token；金额保持未知。短连通探测与 NOP 检查不构成模型性能测试。

## 7. 本地代码检查

```bash
.venv-harbor/bin/python -m unittest discover -s tests -v
```

测试使用临时合成记录、localhost 模拟 API 和临时绘图目录，不读真实凭据、不调用外部模型。CI 执行同一套检查。安装并下载 AppWorld 后，还可检查真实世界初始化、持久执行和空操作评分：

```bash
.venv-harbor/bin/python scripts/smoke_appworld.py
```

Docker/固定运行时就绪后可执行无模型的容器评分检查：

```bash
.venv-harbor/bin/python scripts/smoke_nop.py
```

## 8. 上传到 GitHub

建议上传本目录自身作为仓库根目录。`.gitignore` 已排除运行结果、数据集、凭据、缓存、依赖和私有运行时；上传前仍检查 `git status --short`。

```bash
# 如果下载的是 ZIP，先 git init；若目录已有 .git，不需重新初始化
git init -b main
git add .
git diff --cached --stat
git commit -m "Add config-driven single-model cross-harness benchmark"
git remote add origin https://github.com/<YOUR_ACCOUNT>/<YOUR_REPO>.git
git push -u origin main
```

在 GitHub 创建一个空仓库后替换上述地址；若已配置 origin，使用 `git remote set-url origin ...`。上传不需要提交 `.env`、`runs/` 或任何实验成绩。

第三方项目和数据来源见 [NOTICE.md](NOTICE.md)。
