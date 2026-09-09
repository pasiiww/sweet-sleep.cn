# QQ 知识库检索 Demo

使用腾讯官方 [`qq-botpy`](https://github.com/tencent-connect/botpy) 1.2.1，监听 `public_messages` 中的 QQ 私聊消息和群 @消息。收到问题后调用既有知识库 `/knowledge/api/retrieve`，直接返回最多 3 个原文片段和来源，不调用 LLM 或 Embedding。

## 使用

- 私聊直接提问；群聊 @机器人后提问。
- 支持 `/检索 问题`、`/搜索 问题`、`/search question`。
- `/help` 或 `/帮助` 显示说明。
- 无结果、超长问题、繁忙和检索失败会给出明确提示。
- 每段最多 360 字，较长内容截断；只回复一个文本消息。

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
