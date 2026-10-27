# OOPZ 网页自动加入语音频道：研究记录

日期：2026-10-27。目标是把“打开目标域、定位语音频道、确认已连接”做成可重复的辅助流程，同时不读取登录 token、不调用未公开协议，也不修改 OOPZ 内部状态。

## 当前结论

- 已登录网页中，目标域能够通过公开 UI 打开。
- 语音频道自动加入仍未成功。
- 右键菜单没有稳定的“加入语音”项。
- 通过浏览器坐标、原生应用坐标、可访问性节点和 Playwright 执行默认双击，均未同时得到“目标成员出现在频道”和“页面显示已连接”的证据。
- `voice_open` 只打开本机音频设备，不负责网页入房。

因此当前生产路径仍要求用户在 OOPZ 网页手动双击目标语音频道。

## 已确认的界面事实

| 项目 | 观察 |
|---|---|
| Flutter 可访问性 | 页面默认可能只暴露 `Enable accessibility`；启用后才出现较完整的语义节点。 |
| 单击与双击 | 单击可选中频道；双击提示和业务入房动作并不等价。 |
| 语义激活 | 对节点执行可访问性激活可能只触发单击处理，不能自动调用 Flutter 的双击识别器。 |
| 右键菜单 | 已检查的频道菜单没有直接加入语音的入口。 |
| 公开文档 | 未找到可引用的 OOPZ 官方自动入房 API 或指定语音频道深链规范。 |

## Flutter 技术线索

Flutter 的双击识别使用 `kDoubleTapMinTime = 40ms` 与 `kDoubleTapTimeout = 300ms`。自动化工具的默认 `dblclick` 可能以 0 间隔发送两个点击，也可能与 Flutter 手势竞争、命中位置或焦点状态冲突。

可访问性激活不等于业务双击。Flutter 的默认语义 tap 处理器主要提取单击识别器；若应用把单击绑定为选中、双击绑定为入房，语义激活可能只能选中频道。

参考资料：

- [Flutter Web accessibility](https://docs.flutter.dev/ui/accessibility/web-accessibility)
- [Flutter gestures](https://docs.flutter.dev/ui/interactivity/gestures)
- [kDoubleTapMinTime](https://api.flutter.dev/flutter/gestures/kDoubleTapMinTime-constant.html)
- [kDoubleTapTimeout](https://api.flutter.dev/flutter/gestures/kDoubleTapTimeout-constant.html)
- [Playwright mouse API](https://playwright.dev/docs/api/class-mouse#mouse-dblclick)
- [OOPZ 频道设置帮助](https://help.oopz.cn/42f9/3020)

## 下一次最小验证

1. 复用已登录的原浏览器标签页，先确认当前页面和目标频道，不刷新、不重建页面。
2. 如果只看到 `Enable accessibility`，通过公开按钮启用语义树。
3. 使用真实指针输入，在一次连续动作中控制两次点击间隔约 100 ms，再核对频道成员和连接状态。
4. 若控制点击间隔的工具不可用，再测试明确聚焦后的 Enter 或 Space，不在聊天输入框或焦点不明时发送按键。
5. 检查 UI 是否提供可复制的频道分享链接。只使用界面实际生成的值，不按内部 ID 猜 URL。
6. 至少验证三条独立证据：目标成员出现在语音频道、页面显示连接状态、`voice_open` 前后音频会话仍按预期建立。
7. 连续做三次“未连接 → 自动连接 → 核对目标频道”，记录成功和失败次数。

## 不采用的路径

- 不读取 Cookie、localStorage、WebSocket frame 或登录 token。
- 不伪造内部 WebSocket/HTTP 请求。
- 不直接修改 Flutter 内部状态。
- 不根据历史截图坐标盲点。
- 不把频道高亮、点击成功或 `voice_open` 成功当成已入房。

自动入房目前是独立研究项，不改变五个 MCP 音频工具的生产语义。
