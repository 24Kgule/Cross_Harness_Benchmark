# 模型网关配置

公共配置由 `configs/api.active.json` 控制，真实模型名、API 地址和 key 由仓库根 `.env` 的 `model_name`、`url`、`NEW_API_KEY` 提供。`experiment_api.config.load_settings()` 是唯一的提供方入口，不读取用户全局 harness 配置或其它旧 key。

原生 CLI → 容器 loopback HTTP relay → 只读挂载的 Unix socket → 宿主网关 → 配置的模型服务。

| Harness | 服务端需要支持的接口 |
|---|---|
| Codex | `/v1/responses`，Responses SSE、工具调用 |
| Claude Code、dsh、Kimi Code | `/v1/messages`，Messages SSE、thinking / adaptive thinking |
| OpenCode、Pi | `/v1/chat/completions`，Chat SSE、reasoning、工具调用 |

网关不在本地转换三种协议；服务端是否转换需由服务提供方说明。模型请求固定为一个配置标识，响应回显必须在 `accepted_response_models` 中；这些别名不能代表不同权重。模型回显检查仅核对服务端声明，不能独立验证实际权重。

请求上限在所有 CLI 之外再次检查。输出上限按客户端原始值与公共上限取较小者；保存原始/生效上限、推理配置、工具名称与 schema hash、请求时长、首内容/首文本、完成标志和 usage。不保存模型正文、提示或工具参数到网关请求日志。原生 CLI 日志仍可能包含任务数据，位于本地忽略的 `runs/`。

宿主真实 key 不挂载到容器。某些原生 CLI 需要名为 `DEEPSEEK_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 的环境变量；启动器填入的只是逐任务本地代理 token。`experiment_api/private/master.txt` 和 AppWorld ready 文件只留宿主。公共模型配置、请求日志和 repo ZIP 都不应包含真实 key。

`runtime.py` 只生成公共运行配置和只读挂载清单；`harbor_agent:RelayNativeAgent` 使用这些挂载启动各 CLI。`gateway.py` 负责认证、模型约束、输出上限、观测和请求数限制。401/402/403、模型异常会停止新模型分发；实际已在途请求仍可能完成。

使用 `python -m harness_benchmark doctor --config ...` 检查本地配置和 Docker。可选 `python -m experiment_api.probe --output runs/probe-name` 才会实际请求三种接口，产生 token 费用；请先填写正确回显。费率未知时不计算金额。
