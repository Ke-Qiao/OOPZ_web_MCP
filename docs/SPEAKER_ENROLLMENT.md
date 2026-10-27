# 多人声纹注册

CAM++ 声纹库保存在本机私有目录，不包含在仓库中。每位需要响应的人都使用独立、稳定的 `speaker-id` 注册；注册成功后，服务端只允许已确认的注册者触发正文、回复和打断。

## 准备

1. 关闭正在运行的 live 语音会话。
2. 确认 CAM++ 与 Smart Turn 模型路径、SHA-256、注册库路径已经写入私有配置。
3. 确认网页输出路由到 `OOPZ RX`。
4. 注册时现场只让目标人员讲话，其他人保持安静，也不要播放音乐或助手回复。

注册库为空时，启用声纹的 live profile 会拒绝打开，不会自动退化成接收所有人。仅做显式无身份测试时才可设置：

```toml
[profiles.oopz.speakers]
enabled = false
```

关闭声纹后所有人声都标为 `unverified`，仍会运行 Smart Turn。

## 从 RX 现场注册

```sh
uv run oopz-mcp-speaker --config config.local.toml --profile oopz enroll \
  --speaker-id user01 --name "User 01" --record
```

命令执行三轮、每轮 8 秒采样。每轮先等待 Enter，再打开配置指定的 RX。请使用将来实际交流时的同一麦克风位置、路由和接近的音量，连续说不同的自然句子。三轮和一致性检查通过后才保存归一化声纹向量。

注册命令只读取 RX，不打开 TX，不连接讯飞或 TTS。原始样本只保留在内存，不会自动写入 WAV。

## 从已有 WAV 注册

每份 WAV 必须为 2–15 秒、16 kHz、单声道、未压缩 signed 16-bit PCM。每人提供 3–16 份内容不同的样本：

```sh
uv run oopz-mcp-speaker --config config.local.toml --profile oopz enroll \
  --speaker-id user01 --name "User 01" \
  --wav /path/to/user01-1.wav \
  --wav /path/to/user01-2.wav \
  --wav /path/to/user01-3.wav
```

命令不会把 WAV 复制进注册库，也不会删除原文件。

## 查看与删除

```sh
uv run oopz-mcp-speaker --config config.local.toml --profile oopz list
uv run oopz-mcp-speaker --config config.local.toml --profile oopz remove \
  --speaker-id user01
```

`list` 只输出 ID 和显示名称，不输出向量。同一 ID 不会被静默覆盖；重新录入前先删除旧 ID。

注册、删除和重录会在下一次 `voice_open` 时生效。已打开会话使用启动时的注册库快照。

## 存储与隐私

- 默认注册库：`.runtime/speakers/registry.json`
- 文件权限：`0600`
- 保存内容：ID、显示名称、归一化向量和模型指纹
- 不保存：注册原始录音
- 不入库：模型权重、个人配置、日志正文

CAM++ 是统计模型。样本不足、噪声、回声和重叠讲话仍可能导致漏识别或错认。注册库不能当作 OOPZ 账号认证，也不能替代说话对象判断或抗重放机制。
