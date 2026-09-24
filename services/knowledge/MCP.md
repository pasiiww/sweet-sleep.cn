# Sweet Sleep MCP

`mcp_server.py` implements MCP over newline-delimited JSON-RPC on standard input/output. It uses only Python's standard library and the existing knowledge service modules. Run it on the knowledge server; it reads `KB_ADMIN_TOKEN` and the database directory from `/etc/sweet-knowledge.env`. Never place the admin token in a client config or send it through MCP arguments.

## Connect a local MCP client

Add this entry to the client's MCP configuration. Replace `root@YOUR_SSH_ALIAS` with the SSH host alias already configured on your machine:

```toml
[mcp_servers.sweet_sleep]
command = "ssh"
args = ["-T", "root@YOUR_SSH_ALIAS", "python3", "/opt/sweet-knowledge/mcp_server.py"]
startup_timeout_sec = 20
```

The client opens a standard input/output SSH session for the MCP process. The server script itself stays silent except for MCP protocol responses. SSH access is the access boundary; anyone who can start this server as root can use knowledge write tools.

## Tools

- `list_recent_chat_groups`, `get_recent_chat_messages`: read member chat text retained by the learning service (up to seven days), with group, time, keyword, member-ID, and page limits. `limit` sets the number of messages; `offset` pages older results.
- `pin_chat_member`, `unpin_chat_member`, `list_pinned_chat_members`: persist up to 100 pinned member IDs per group. Get a `member_id` from the recent-chat result, then pin it with an optional label. Call `get_recent_chat_messages` with `pinned_only: true` to view only pinned members. You can instead pass `member_ids` to inspect selected members without pinning them.
- `list_knowledge_bases`, `get_knowledge_base`, `create_knowledge_base`, `update_knowledge_base`, `delete_knowledge_base`.
- `search_knowledge`, `search_documents`, `get_document`, `create_document`, `update_document`, `delete_document`.
- `search_ba_wiki`: 按角色或问题检索蔚蓝档案资料。`source: "auto"` 先查 GameKee，未命中时查日文 Blue Archive Wikiru；也可用 `gamekee` 或 `bluearchivewiki` 指定来源。每次最多返回4条，只读、不保存图片或页面到磁盘，并附来源链接。
- `search_qa`, `get_qa`, `create_qa`, `update_qa`, `delete_qa`.

Write tools mutate the active knowledge database directly through its existing validation, indexing, and revision paths. Deletes are permanent. Search and read tools never modify the database. Chat history access is read-only and bounded to seven days and at most 400 messages per call. Pinned-member filters are scoped to one group.

## Install or update on the server

Copy `mcp_server.py` and `ba_wiki.py` to `/opt/sweet-knowledge/`. No service restart is required: the MCP client launches one process per connection over SSH. Verify the client can initialize and call `search_ba_wiki` with a character name after connecting.
