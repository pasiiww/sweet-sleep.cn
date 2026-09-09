# QQ 知识库客服机器人

使用腾讯官方 [`qq-botpy`](https://github.com/tencent-connect/botpy) 1.2.1，监听 `public_messages` 中的 QQ 私聊消息和群 @消息。收到问题后调用知识库 `/knowledge/api/answer`，由知识库服务读取后台最新配置、检索资料并调用 DeepSeek。QQ 进程不读取模型密钥。

## 使用

- 私聊直接提问；群聊 @机器人后提问。
- 支持 `/检索 问题`、`/搜索 问题`、`/search question`。
- `/help` 或 `/帮助` 显示说明。
- 无结果、超长问题、繁忙和检索失败会给出明确提示。
- 正常时生成2–5个检索词、合并去重检索内容，再直接回复客服回答，不附加引用列表；模型未配置、关闭、余额不足、密钥无效或超时时只回复最相关文档片段。资料无命中或模型判定依据不足时转人工。
- `/身份` 或 `/whoami`：群内显示群 OpenID 和发送者成员 OpenID，供管理员配置人工联系人；不会给发送者授予管理员身份。
- 仅在群聊转人工时，程序从后台配置中选择当前群的管理员，构造 `qqbot-at-user` 提及标签；模型输出及知识库正文里的提及标签会转义。无配置或私聊时仅提示联系管理员。实际提及需配置真实成员并在对应群验收。

## 配置与部署

生产代码 `/opt/sweet-qqbot/bot.py`，虚拟环境 `venv/`；root 私有配置 `/etc/sweet-qqbot.env`：

```ini
QQ_APP_ID=<appid>
QQ_APP_SECRET=<secret>
QQ_SANDBOX=false
KB_ID=<明确指定的一个知识库ID>
KB_API_URL=http://127.0.0.1:8765/knowledge/api/retrieve
KB_READ_TOKEN=<只读召回密钥>
```

机器人只查询指定知识库，不支持从聊天中切换库、执行命令或修改知识。具有机器人会话访问权限的人可以查询绑定库的内容。QQ 平台侧的测试成员、可用群聊、消息事件权限和发布状态以开发后台实际配置为准。

当前配置绑定「午觉糖水铺客服知识库」。演示说明明确标为演示，不代表店铺营业时间、价格或售后政策。添加真实资料后立即可检索；修改绑定 ID 后重启机器人。

```sh
python3 -m venv /opt/sweet-qqbot/venv
/opt/sweet-qqbot/venv/bin/pip install -r requirements.txt
# 将 bot.py 放入 /opt/sweet-qqbot，环境配置 chmod 600
# 创建 sweet-qqbot 系统用户后安装同目录 systemd unit
systemctl daemon-reload
systemctl enable --now sweet-qqbot
journalctl -u sweet-qqbot -n 30 --no-pager
```

采用出站 WebSocket，不新增公网监听端口。SDK 1.2.1 的 HTTP 和 WebSocket 使用裸 `SSLContext()`，本应用在启动时将两个模块的构造函数替换为 `ssl.create_default_context`，启用 CA 与主机名验证；更新 SDK 时应重新核验。禁用 SDK DEBUG 和文件日志，日志不记录正文和密钥。

同一事件在 SQLite 中去重 24 小时，处理前记录，重启后仍生效。使用固定 `msg_seq=1` 防止重发重复回复；进程在处理中崩溃可能丢失该次回复，用户可重新发问。最多并发处理 4 次检索。systemd 自动重启、限制启动频率，数据位于 `/var/lib/sweet-qqbot/seen.db`。

测试（没有向真实 QQ 用户发消息）：

```sh
/opt/sweet-qqbot/venv/bin/python -m unittest discover -s services/qqbot -v
```

覆盖两种事件、去重、原文格式、无结果、错误和 HTTP 请求合同。真实收发验收需用户向机器人发问，并检查 `QQ_CONNECTED`、`REPLY_OK` 日志。

## 客服配置

在 `/knowledge/` → 模型设置的「客服回答模型」中配置启用开关、DeepSeek API Key、模型名、System Prompt 及各群的人工联系人。保存后下一条消息生效，不需要重启 QQ 服务。默认模型 `deepseek-v4-flash`，初版提示词在 `services/knowledge/answers.py`，后台可恢复初版。

群联系人每行：`群OpenID 管理员OpenID [第二位管理员OpenID]`。使用对应群内 `/身份` 获取的标识，不要填 QQ 号码或私聊用户 ID。该功能不会自动识别群主身份；后台配置者负责确认接管成员。

历史 `KB_API_URL` 以 `/retrieve` 结尾时自动升级为 `/answer`，其他自定义 URL 按原值使用。知识库 `/answer` 是读取并生成回答的接口，不再只是原文召回，使用共享只读密钥鉴权；该密钥持有者可触发已启用模型的调用费用。超时上限：每次模型调用20秒（关键词与回答各一次），机器人知识库请求55秒。

普通群消息 `on_group_message_create` 直接忽略；只有平台的 `on_group_at_message_create` 事件可进入群处理流程，且内部入口再次检查 mention 标志。私聊保持直接处理。
