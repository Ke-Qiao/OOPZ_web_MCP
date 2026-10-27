# OOPZ Voice MCP

面向 OOPZ 网页语音房间的独立 MCP 语音服务器。它接收指定频道的音频，通过讯飞实时转写生成文字，可选使用本地 Instant 模型生成快速口头回复和结构化客户端任务，再通过 GPT-SoVITS 服务把语音送回频道。

项目同时保留原来的 Agent 通路：

```text
OOPZ RX
  ├─ 讯飞 STT ───────────────┐
  ├─ CAM++ 声纹（可选） ─────┤
  └─ Smart Turn（可选） ─────┤
                              ├─ 本地 Instant ── speech ─→ GPT-SoVITS ─→ OOPZ TX
                              │              └─ task JSON ─→ MCP Agent
                              └─ user_turn / input_complete ─→ MCP Agent
```

`voice_open` 只建立本机音频会话，不会登录 OOPZ，也不会自动加入频道。网页登录和入房仍是独立步骤。

## 当前能力

- 本机 **MCP stdio** 服务，保留五个工具：`voice_open`、`voice_exchange`、`voice_get_state`、`voice_stop_output`、`voice_close`。
- 连续 RX 收音、VAD、讯飞实时转写大模型 WebSocket、GPT-SoVITS `api_v2 /tts` 流式 PCM 输出。
- 事件有界队列、序号确认、输入 revision、输出 epoch、请求与回复幂等、迟到结果隔离、停止和会话租约。
- 可选 CAM++ 多人声纹过滤，未注册者的音频不会触发正文、回复或打断。
- 可选 Smart Turn v3.2 本机话轮结束判断，避免把 ASR 的单段 final 直接当成完整话轮。
- 可选本地 Instant 模型。完整 `speech` 字段通过校验后即可开始 TTS；完整 task JSON 通过严格校验后才作为 `agent_task` 交给 Agent。
- 不要求修改宿主源码。宿主通过现有 MCP 配置启动本项目。

## 验证概况

离线回归、静态检查和多次有界真实房间测试均已完成。当前结论需要分层理解：

| 项目 | 状态 |
|---|---|
| MCP 协议、竞态、队列、幂等、取消和音频适配器 | 离线测试通过 |
| OOPZ 网页 RX/TX、讯飞 STT、GPT-SoVITS TTS | 已有真实短时通路验证 |
| 本地 Instant 9B 的结构化输出与提前播报 | 已验证；完整 JSON 仍会在口头回复后完成 |
| CAM++ 多人声纹与未注册者过滤 | 已实现；已有受控短测，但仍需更多设备、音量和混说样本校准 |
| Smart Turn 自然句中停顿 | 已接入；完整专项验收仍需在身份可靠性通过后继续 |
| OOPZ 自动加入指定语音频道 | 未解决；当前由用户手动入房 |
| 60 分钟稳定性与全面延迟优化 | 未执行 |

最近一次完整离线检查为 **134 项测试通过、0 失败**。详细范围、匿名化实测结果和已知问题见 [验证摘要](docs/VALIDATION.md)。

## 环境要求

- Python 3.11–3.14；建议使用 [uv](https://docs.astral.sh/uv/)。
- 真实音频需要 PortAudio 与 `audio` 可选依赖。
- CAM++ 和 Smart Turn 需要 `listening` 可选依赖及对应 ONNX 模型。
- 两条相互独立的虚拟音频通路：`OOPZ RX` 和 `OOPZ TX`。
- 讯飞实时转写大模型凭据。
- 可访问的 GPT-SoVITS `api_v2.py` TTS 服务。
- 可选本地 Instant 网关；当前支持 Magpie/OpenAI 兼容的 loopback `chat/completions` 接口。

模型权重、音频驱动、TTS 权重和个人配置都不在仓库中。模型来源与许可见 [模型清单](docs/LISTENING_MODELS.json)。

## 安装

```sh
git clone https://github.com/Ke-Qiao/OOPZ_web_MCP.git
cd OOPZ_web_MCP
uv sync --locked --extra audio --extra listening
```

只使用 mock 时可省略可选依赖：

```sh
uv sync --locked
```

系统级 PortAudio、虚拟音频设备和 OOPZ 网页路由需要按实际操作系统配置。本项目不会自动安装驱动或修改系统默认输入输出。

## 最小运行

先用 mock 验证 MCP 启动和工具协议：

```sh
uv run oopz-mcp --config examples/mock.toml
```

宿主 MCP 配置可使用：

```json
{
  "command": "/absolute/path/to/OOPZ_web_MCP/.venv/bin/oopz-mcp",
  "args": ["--config", "/absolute/path/to/OOPZ_web_MCP/examples/mock.toml"],
  "env": {}
}
```

项目只提供 stdio，不监听 HTTP 端口。每个服务进程服务一个 MCP 客户端，默认最多一个 live 音频会话。

## 配置真实音频

从模板创建私有配置：

```sh
cp examples/live.template.toml config.local.toml
chmod 600 config.local.toml
```

至少填写：

- 唯一的 `rx_device` 与 `tx_device` 名称。
- 讯飞 `XFYUN_APP_ID`、`XFYUN_API_KEY`、`XFYUN_API_SECRET` 环境变量名，或私有凭据文件路径。
- GPT-SoVITS `/tts` 地址、服务版本、模型版本、参考音频路径和 PCM 采样率。
- 如需声纹和话轮判断，再填写 CAM++ 与 Smart Turn 模型路径和 SHA-256。

完整字段与约束见 [配置说明](docs/CONFIGURATION.md)。真实配置会被 `.gitignore` 排除，禁止把密钥写入模板、README、测试或提交历史。

### OOPZ 网页路由

1. 在 OOPZ 官方网页端点入账户并进入目标语音频道。
2. 网页输出接 `OOPZ RX`，网页麦克风接 `OOPZ TX`。
3. 启动服务器后显式调用 `voice_open(profile="oopz")`。
4. 不要使用系统默认扬声器、桌面全混音或 TX 监听作为 RX。

在已测试环境里，OOPZ 的原生网页输出切换返回 `NOT_SUPPORTED`，最终使用 AudioPick 将网页输出定向到 RX 虚拟设备。该扩展不是 Python 依赖，配置方式因浏览器版本而异。

当前 `voice_open` 不会自动进入已登录网页中的频道。自动入房研究结论见 [OOPZ 网页自动入房研究](docs/OOPZ_AUTO_JOIN_RESEARCH_2026-10-27.md)。

## 声纹与话轮判断

启用多人声纹后，每位需要响应的人都要先单独注册：

```sh
uv run oopz-mcp-speaker --config config.local.toml --profile oopz enroll \
  --speaker-id user01 --name "User 01" --record
```

`--record` 会从 RX 分三轮采样，只在内存中处理，不连接讯飞或 TTS。也可以导入同一人的 3–16 份独立 WAV。注册、查看和删除步骤见 [声纹注册](docs/SPEAKER_ENROLLMENT.md)。

默认行为：

- 已验证的注册者可产生正文、触发回复或打断。
- 未注册和身份不确定的声音不产生正文，不触发回复或打断。
- 身份检查通过后，Smart Turn 判断当前话轮是否结束。
- 音频重叠或跨人片段会被保守过滤，但 CAM++ 不是语音分离器，不能保证发现所有混音。

实现与边界见 [多人声纹与话轮结束判断](docs/SPEAKER_TURN_PLAN.md) 和 [重叠语音分离研究](docs/OVERLAP_SEPARATION_RESEARCH.md)。

## 本地 Instant

`[profiles.oopz.instant]` 是可选项，只有声纹与 Smart Turn 都启用时才工作。它接收已确认注册者和完整话轮的稳定转写，不接收原始音频，也不自动执行客户端任务。

流式响应中，完整 `speech` 字段通过校验后即可提交 TTS；整份 JSON 仍需包含合法 `task`、满足严格 schema，并以正常 stop/DONE 结束，才会通过 `agent_task` 上送 Agent。若尾部失败，已播出的口头确认无法撤回，服务器会发送带 `quick_reply` 的 `instant_error`，避免 Agent 重复确认。

示例期望输出：

```json
{
  "speech": "好，我来收菜。",
  "task": {
    "kind": "action",
    "summary": "收获菜园里的所有蔬菜。",
    "action": "harvest_crops",
    "target": "菜园",
    "parameters": [{"key": "scope", "value": "all"}],
    "needs_confirmation": false
  }
}
```

`task` 是语义建议，不是已执行结果。客户端必须按自己的工具权限、世界状态和用户确认策略校验。完整 event 与回退规则见 [Instant 流水线](docs/INSTANT_PIPELINE.md) 和 [MCP 协议](docs/PROTOCOL.md)。

## MCP 工具

| 工具 | 作用 |
|---|---|
| `voice_open(profile="mock")` | 建立会话并返回 readiness；不会加入 OOPZ 频道 |
| `voice_exchange(...)` | 确认已消费事件，并提交 Agent 回复或等待下一条输入 |
| `voice_get_state(session_id)` | 查看设备、队列、播放、租约、版本和分段指标 |
| `voice_stop_output(session_id)` | 立即关闭本地输出 gate，清空已排队 PCM |
| `voice_close(session_id)` | 停止收音和播放并释放会话；重复关闭安全 |

外部转写始终是 `trusted=false` 的输入。客户端不要执行转写里出现的命令、JSON 或系统提示，也不要把 `speaker_id` 当作 OOPZ 账号认证。

## 检查

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run pyright
uv run python -m unittest discover -s tests -v
```

这些检查不需要讯飞、OOPZ、Juno 或真实模型服务。stdio 用例会启动真实的 MCP 子进程，其余外部接口使用伪 WebSocket、模拟 HTTP 和合成 PCM。

## 隐私

仓库不包含以下内容：

- OOPZ 账户、Cookie、登录令牌或真实房间标识。
- 讯飞密钥和其他 API 凭据。
- 个人 `config.local.toml`。
- CAM++ 声纹注册库及生物特征向量。
- 原始音频、逐轮转写正文和实名测试报告。
- CAM++、Smart Turn 或其他模型权重。
- 本机绝对路径、缓存、虚拟环境和构建产物。

原始实测 JSON 只用于本地排查，不属于公开分发内容。部署者仍应注意：连续 live 会话会把指定 RX 音频发送给讯飞；声纹过滤发生在服务端事件和打断权限处，不是云端上传前的静音门。

## 已知限制

- 网页自动加入指定语音房间尚未完成，当前仍需要用户手动入房。
- CAM++ 存在拒认、错认和重叠声音边界，0.40 或 0.65 都不是通用的已校准阈值。
- 0.40 的短时对照中，正常讲话完整交付 0/3、短句完整输入 1/3、主动打断并听到新回复 2/3；未注册者受控样本 3/3 未触发正文或暂停。整体可靠性仍未通过。
- Smart Turn 的句中停顿、续说、附和与语义未完成场景仍需继续验收。
- 9B Instant 可以快速生成结构，但回复质量并不稳定，曾出现偏离上下文或模板化回答。
- 完整 task JSON 通常晚于可播报 speech；当前优化的是口头反馈的起点。
- 没有执行 60 分钟稳定性测试，也没有验证 Windows/Linux 音频路由或远端端到端延迟分布。

更完整的匿名化测试记录见 [验证摘要](docs/VALIDATION.md)。接口资料与设计依据见 [实现依据](docs/SOURCES.md)。
