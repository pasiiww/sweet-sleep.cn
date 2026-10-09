# Sweet Sleep MCP

`mcp_server.py` implements MCP over newline-delimited JSON-RPC on standard input/output. It uses only Python's standard library and the existing knowledge service modules. Run it on the knowledge server; it reads `KB_ADMIN_TOKEN` and the database directory from `/etc/sweet-knowledge.env`. Never place the admin token in a client config or send it through MCP arguments.

## Connect a local MCP client

Add this entry to the client's MCP configuration. Replace `root@YOUR_SSH_ALIAS` with the SSH host alias already configured on your machine:

```toml
[mcp_servers.sweet_sleep]
command = "ssh"
args = ["-T", "root@YOUR_SSH_ALIAS", "/opt/sweet-knowledge/venv/bin/python", "/opt/sweet-knowledge/mcp_server.py"]
startup_timeout_sec = 20
```

The client opens a standard input/output SSH session for the MCP process. The server script itself stays silent except for MCP protocol responses. SSH access is the access boundary; anyone who can start this server as root can use knowledge write tools.

## Tools

- `list_recent_chat_groups`, `get_recent_chat_messages`: read member chat text retained by the learning service (up to seven days), with group, time, keyword, member-ID, and page limits. `limit` sets the number of messages; `offset` pages older results. Results include the sender nickname when available, capped at 12 characters.
- `pin_chat_member`, `unpin_chat_member`, `list_pinned_chat_members`: persist up to 100 pinned member IDs per group. Get a `member_id` from the recent-chat result, then pin it with an optional label. Call `get_recent_chat_messages` with `pinned_only: true` to view only pinned members. You can instead pass `member_ids` to inspect selected members without pinning them.
- `list_knowledge_bases`, `get_knowledge_base`, `create_knowledge_base`, `update_knowledge_base`, `delete_knowledge_base`.
- `search_knowledge`, `search_documents`, `get_document`, `create_document`, `update_document`, `delete_document`.
- `search_knowledge` 可传 `original_query` 保留用户完整问题；`query` 可为改写后的检索词。与 Agent 共用融合/重排和商品约束；保留 `results`、`context`、`query`、`kb_id` 等返回字段，`top_k` 支持1–20。
- `search_ba_wiki`: 按角色或问题检索蔚蓝档案资料。`source: "auto"` 先查 GameKee，未命中时查日文 Blue Archive Wikiru；也可用 `gamekee` 或 `bluearchivewiki` 指定来源。每次最多返回4条，只读、不保存图片，文本与 JSON 响应以 SQLite 缓存最多30天、容量不超过12 MiB，并附来源链接。
- `search_qa`, `get_qa`, `create_qa`, `update_qa`, `delete_qa`.
- `recall_group_message`: 撤回指定群消息。需要机器人有对应群管理权限；只允许管理员通过 MCP 调用。
- `send_group_warning`: 向指定群消息发送固定的友善交流提醒；后台“性骚扰提醒”开关关闭时不会发送。提醒引用目标消息；批量操作中要排在撤回之前。
- `record_harassment_count`: 按群和消息 ID 幂等记录敏感词/性骚扰命中及待审核候选词，不保存消息正文。
- `execute_admin_actions`: 一次 MCP 调用按顺序执行1至8项知识库文档/QA写入、命中计数、撤回或开关控制的提醒。逐项返回结果；某项失败不会撤销已完成操作，也不会停止后续操作。

Write tools mutate the active knowledge database directly through its existing validation, indexing, and revision paths. Deletes are permanent. Search and read tools never modify the database. Chat history access is read-only and bounded to seven days and at most 400 messages per call. Pinned-member filters are scoped to one group.

群消息撤回与提醒通过仅监听 `127.0.0.1:8766` 的机器人控制接口执行，不向公网开放。该接口要求 `/etc/sweet-knowledge.env` 与 `/etc/sweet-qqbot.env` 配置相同的 `KB_MODERATION_TOKEN`；不要把此密钥传入 MCP 参数。性骚扰短语（仅明确 @机器人时检查）或后台敏感词命中后，先记录命中再尝试撤回；同一成员在同一群10分钟内累计3次后默认发送提醒。后台可独立设置自动禁言开关、触发次数（1–20次）和禁言时长（1–1440分钟）；自动禁言默认关闭，开启后达到设置次数才尝试禁言，失败时仅在提醒开关开启时回退为提醒。关闭提醒不会关闭命中统计或撤回。MCP 仍不向模型提供禁言工具；`send_group_warning` 和批量操作中的提醒均受后台提醒开关控制。计数不依赖撤回结果，历史敏感词撤回计数也保留在汇总中。未配置的性骚扰短语作为候选词送后台审核，只有管理员批准后才加入自动敏感词表。MCP 管理工具与自动处置共用 QQ 接口限速。

## Install or update on the server

Deploy the matching knowledge-service modules together, including `mcp_server.py`, `tool_service.py`, `execution_budget.py`, `ba_wiki.py`, and their server dependencies to `/opt/sweet-knowledge/`; restart `sweet-qqbot` when updating its control API. Restart `sweet-knowledge` when shared service modules change. Changes only to the MCP adapter do not need a knowledge-service restart: the MCP client launches one process per connection over SSH. Verify the client can initialize and list tools after connecting.
