# 实现依据

提供的设计输入：

- 独立 OOPZ MCP 设计说明，v0.2，2026-10-23。
- 对应实现任务说明。

设计输入要求独立服务器、五个工具和音频内部控制，不授权修改宿主。实现先完成通用服务器与模板，之后在用户明确许可时才执行离线和真实音频验证。

本轮查阅的官方资料：

- [MCP Python SDK v1 维护分支](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)；实际安装与静态核对的版本由 `uv.lock` 固定为 1.30.0。
- [MCP Python SDK 官方文档](https://py.sdk.modelcontextprotocol.io/v1/)；stdio 和工具生命周期采用 SDK，未自行混用不同版本的 wire schema。
- [sounddevice 流式 API](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)；输入/输出设备显式选择，回调与模型/网络分离。
- [讯飞实时转写大模型协议](https://www.xfyun.cn/doc/spark/asr_llm/rtasr_llm.html) 与其中下载的官方 Python 示例；核对签名、会话 ID、40 ms PCM 和结果结构；使用本项目适配器完成真实鉴权与转写验证，没有直接运行下载的示例。
- [websockets asyncio 客户端 API](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)；锁定安装版本 17.3，核对连接、缓冲、日志和关闭参数。
- [GPT-SoVITS 官方 api_v2.py](https://github.com/RVC-Boss/GPT-SoVITS/blob/main/api_v2.py)；按其 `/tts` JSON 参数和 raw 流式返回实现，实际外部部署须填写自身 revision 与模型版本，main 链接不是固定的部署版本。
- [OOPZ 官方语音帮助](https://help.oopz.cn/42f9/c43a)；用于确认用户在客户端选择音频设备及说话模式的入口，未使用未验证的 Bot API。

资料说明 API 的用途与签名，不证明本机接线、模型能力、宿主连续循环或任何延迟目标已经通过验证。

网页路由排查参考：

- [AudioPick 官方源码](https://github.com/rain-fighters/AudioPick) 与 [官方扩展商店条目](https://chromewebstore.google.com/detail/audiopick/gfhcppdamigjkficnjnhmnljljhagaha)：可作为浏览器按站点选择音频输出设备的参考；安装前应检查扩展权限和版本。
- [Agora Web SDK LocalAudioTrack 接口](https://api-ref.agora.io/en/video-sdk/web/4.x/interfaces/imicrophoneaudiotrack.html)：辅助解释输出切换失败日志，不把日志当作完整厂商源码缺陷证明。
- [Chrome AudioContext 输出设备选择](https://developer.chrome.com/blog/audiocontext-setsinkid)：网页音频输出能力参考。
