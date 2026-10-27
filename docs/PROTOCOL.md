# 协议语义与错误码

只暴露 `voice_open`、`voice_exchange`、`voice_get_state`、`voice_stop_output`、`voice_close`。MCP 消息封包、初始化和协议版本协商使用官方 SDK。可选本地 Instant 模型生成简短应答及任务建议，仍不提供执行工具；没有 sampling 路径、逐帧 PCM 工具或唤醒宿主的假定。

## 会话和取消

一个 stdio 进程对应一个客户端连接。会话 ID 为进程内随机 UUID 标识，不能拿到另一个进程复用；服务无网络监听。`voice_open` 不幂等，丢失打开结果时不要无限重试；默认容量 1 会明确拒绝重复创建，租约可以回收无人管理的会话。

exchange 在进入等待前以同步状态预约唯一等待者，等待时不持控制锁。重复未完成请求报 `request_in_progress`，另一个新请求报 `exchange_busy`；停止/关闭不排在等待者后面。

工具取消立即关当前输出 gate，记录该 request 为 `cancelled_no_replay`，保留会话到租约到期或显式关闭。客户端 stdin EOF 先关闭所有 gate，再等待 SDK/后台清理，避免模型加载期间延迟停止其他会话。会话关闭先停输出，再释放设备和 HTTP 连接。原生 ASR 线程最多等待 1 秒收尾；已经进入原生推理的工作可能继续至当前调用返回，不能声明强制终止了 GPU/CPU 或远端 TTS。

## 交付与确认

- `seq` 对所有事件单调递增；同一条事件可能被多次交付，调用方以 seq 去重。
- `next_event_seq` 是已交付高水位。调用方消费返回事件后，下一次请求用 ack 确认；客户端失去响应时不应猜测服务器内部序号。
- ack 不可倒退，也不可超过服务器已交付高水位。原样重试先查请求缓存，不重新应用旧 ack。
- 事件缓存满时丢掉最旧的未确认事件，下一次结果先包含 `event_gap`，其 seq 等于缺口末端，随后是仍保留的事件；确认 gap 表示知道内容已缺失。
- 缓存达到 `request_capacity` 时拒绝新请求，保留已有记录以允许原样重试。不会因为 LRU 淘汰而重新接受旧 ID。
- `reply_id` 去重和 `request_id` 去重均限当前会话。回复已受理后即使中断、失败，也不会用同一 reply_id 重播；明确决定重试新语音时需新 reply_id，并重新核对输入版本。

返回 `timeout` 不带伪造的用户事件，不自动合成“我在听”。回复 `accepted` 是对 TTS/播放计划的受理，不是播放完成或远端听到的证明。

## 输入与输出版本

每个确认的相关文本段递增全局 `input_revision`，生成 `u<revision>` 的 turn_id。长文本可拆成多个 part，共享同一 revision/turn_id；持续长发言的各个 ASR 段则更新 revision，`segment_final=false` 表示用户尚未结束。启用 speakers/turn 时，`user_turn` 还带 `speaker_id` 和 `speech_epoch`：前者为本地注册身份，后者区分连续的声学话轮，不等于逐文本段的 revision，也不等于播放 output_epoch。多人共享同一全局输入版本和一条回复通路。

稳定句子可以先交付，待结束条件满足后新增带 `seq`、`turn_id` 和 `input_revision` 的 `input_complete`；它不含新正文、不递增 revision，仍须确认序号。新路径还带 `speaker_id`、`completion_reason="smart_turn"` 和 `completion_probability`，表示注册者对应的稳定结果与语义结束均已满足；未启用模型的旧路径仍按本地静音完成。exchange 的 reason 可以为 `events`，宿主应结合 `speech_pending`、输入是否被阻止以及最新版本决定回复。

回复必须同时匹配最新 revision 与 turn_id；revision 0 时 `reply_to_turn_id=null` 可用于明确授权的开场。启用声纹时，注册者身份已确认但文字尚未交付也会暂停输出，因此不能仅靠旧 revision 检查抢先开口；旧配置仍用能量证据暂停。确认新输入后旧输出 stop 并提升 output_epoch；每次 enqueue 和设备回调都再次核对播放计划，迟到音频不能重新启动。

单独的 `voice_stop_output` 和本地快捷键取消当前输出计划，不会阻止以后收到的新授权回复。结束会话用 close。播放位置只有本地已向驱动提交的样本数，不映射成“远端已经听到的文字”。

## 多人身份、换人和不确定输入

此节只适用于启用 speakers/turn 的 live profile。注册库在打开时读取快照，注册管理走本地 CLI，MCP 仍只有原来的五个工具；未注册或无法确认的声音不会成为有效用户正文。声纹是统计判断，不能当作账号认证或抗重放证明。转写无论归给谁，都仍为外部数据、`trusted=false`。

| 事件 | 含义与客户端处理 |
|---|---|
| `user_turn` | 已通过本地区间归属的稳定正文，含 speaker_id/speech_epoch；正常消费并按最新 revision 决定回复。 |
| `input_complete` | 当前话轮结束条件满足；沿用已有 turn_id/revision，不生成新正文。旧话轮的迟到结束结果不能释放新话轮的语音暂停。 |
| `input_superseded` | 含 speaker_id/speech_epoch，reason 为 speaker_change；另一注册者接话使旧声学话轮结束等待，不宣称旧人已经完整表达完意思。 |
| `historical_input` | 比当前活动轮次或最新已交付输入更旧的 speech_epoch 正文迟到，含 text/speaker_id/speech_epoch/trusted=false；新说话人仅身份已确认、尚无正文也会使旧轮历史化。可补充历史，不递增 revision，也不据此取消新回复或重新回答旧人。 |
| `input_uncertain` | 某轮混合归属、上下文过期或语义结束长期不确定；含 speaker_id/speech_epoch/reason，不伪造完成正文。当前活动轮次不确定时停止输出并设置 input_blocked，等新的可靠正文解除。 |

所有这些事件同样进入 seq/ack 流，不能只确认 user_turn。`voice_get_state` 的 `input_blocked` 表示当前回复是否因不确定输入被阻止；`listening` 提供注册人数、模型名称、当前 speaker_id/speech_epoch、过滤段数与不确定轮次数，不返回声纹向量。

新的稳定正文到达前，`input_uncertain` 后提交匹配旧 revision 的回复会返回 `rejected_input_uncertain`；持续重试相同内容不能解决不确定输入。客户端应继续等待可靠输入，或由用户显式关闭重开会话，不把错误码当作可播报的填充语音。

讯飞 partial 本身不能暂停播放或延长注册者话轮。正常 live 仍连续上传 RX，包括可能的未注册声音；本地按 ASR 起止偏移对齐身份区间。已发现跨人、未确认或混合的 final 会被过滤，不能强行归给最后匹配的人；统计声纹无法保证发现全部重叠说话，首版没有音源分离。

等待语义结束与等待云端 final 分开：稳定结果已齐但 Smart Turn 仍认为未完，到 `turn.max_wait_seconds` 后发 `input_uncertain(reason="semantic_end_uncertain")`；当前注册者真正缺少覆盖其语音的 final 才触发 `xfyun_final_timeout`。切换后的旧轮缺 final 可报告 `asr_result_missing_after_speaker_change`，不让旧轮无限阻止新轮。

## 业务结果与错误

启用 Instant 时新增 `agent_task` 与 `instant_error`，使用相同的 seq/ack 和事件缺口机制，不新增 MCP 工具。`input_complete` 在声纹路径补充 speech_epoch。`agent_task` 含源 revision/turn/speaker/epoch、稳定 task_id、speech、结构化 task 与 quick_reply，固定 `trusted=false`、`execution_status="not_started"`；quick_reply 的 scheduled 只表示安排本地 TTS。流式模式在完整 speech 字段通过校验后提前安排语音，agent_task 仍等待整份 JSON 校验；尾部失败或取消时 instant_error 也可能携带 quick_reply，表示此前安排过确认语，不能据此执行任务或重复播报。`instant_error` 的 fallback 区分 Agent 可处理原始输入与已取消的尝试。客户端必须将原转写和派生任务视为同一个请求、按 task_id 去重，不重复执行或播确认。详细字段、取消与回退规则见 [Instant 流水线](INSTANT_PIPELINE.md)。

业务结果用结构化 JSON；参数/控制冲突由 SDK 包装为 MCP 工具错误。客户端应检查 `isError`。

| 错误或状态 | 处理 |
|---|---|
| `unknown_profile` | 选择配置中已存在的 profile |
| `unknown_session` | 会话已关闭/过期/进程不同，重新获得授权范围内的会话 |
| `server_busy`, `session_capacity`, `devices_in_use` | 等待当前打开完成或关闭已存在会话，不能盲目并行打开 |
| `request_id_conflict`, `reply_id_conflict` | 修正调用方 ID 复用；同 ID 不能更换内容 |
| `request_in_progress`, `exchange_busy` | 等现有 exchange 结束；不要另起多个等待循环 |
| `invalid_ack` | 使用实际消费的已交付序号，不得倒退或跳过未交付事件 |
| `wait_too_long` | 降低 wait_ms，并保证低于实际宿主超时 |
| `request_capacity` | 关闭重开；不要删除去重记录继续旧会话 |
| `rejected_stale` | 消费最新输入，再由宿主重新决定回复 |
| `rejected_input_uncertain` | 当前输入不确定，旧回复被阻止；继续等待新的可靠正文，不盲目重试或假装该轮已完成 |
| `rejected_reply_capacity` | 本会话回复去重记录达到 request_capacity；关闭重开，不删除旧 ID 或自动重播 |
| `cancelled_no_replay` | 原请求被取消；换新请求可继续等待，旧回复不会重播 |
| `event_gap` | 明确承认部分输入丢失，必要时请求用户重述 |
| `output_error: tts_failed` | gate 已关；检查服务/配置，确认后用新 reply_id 重试 |
| `backend_error: audio_input_gap` | 输入缺口会使上下文不可信，会话进入 fault 并清理，需重新打开 |
| `backend_error: xfyun_*` | 讯飞断线、协议异常、结果冲突、音频时间线异常或稳定结果超时；停止并清理会话，检查后显式重开 |
| `xfyun_35001` / `xfyun_35002` / `xfyun_35030` | 服务鉴权、额度、签名重复问题；只返回数字代码，不泄露签名 URL 或服务正文 |
| `xfyun_stream_ended` | 服务主动结束整条流；已收到稳定正文仍保留，不能把已结束的连接当作持续收音 |
| `backend_error: audio_device_stalled` | 设备回调超过 2 秒无进展；检查拔出/权限/驱动 |
| `backend_error: audio_callback_failed` | 会话不可恢复，检查算力/设备/缓冲配置后重新打开 |
| `backend_error: listening_input_gap`, `listening_result_gap` | 本地身份/话轮处理队列出现缺口；停止并清理，不能当作完整输入继续 |
| `backend_error: listening_inference_failed` | 本地模型调用失败或超过等待上限；停止并检查模型和运行时后显式重开 |
| `audio_status` | 输出设备报告状态标志；可能有欠载，不能当作已完成可靠播放 |

配置加载失败/未处理启动错误只向 stderr 输出错误类别，避免把令牌、远端错误正文、转写或参考音频路径写入日志。更详细排查应针对本地配置和服务，不自动打开录音。
