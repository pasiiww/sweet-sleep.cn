# 知屿 · 知识库管理与召回

独立页面 `/knowledge/`，Python 3.11+ 标准库后端。生产数据和凭据位于网站根目录之外；不得把整个仓库复制到公开 web root。

## 能力与边界

- 多知识库及文档增删改查；TXT / Markdown 导入；按字符长度与重叠长度切分。
- 关键词：SQLite FTS5 + BM25，中文字符 / 双字分词，英文词。这里没有安装或连接 Elasticsearch。
- 向量：OpenAI 兼容 Embeddings API；分段向量持久化到 SQLite，查询时精确余弦检索。
- 混合：两路候选以 RRF（k=60）融合；Top K 1–20，上下文预算 100–40000 字符。
- 文档、分段配置或模型更新后旧向量失效；手动生成向量，每请求处理最多 32 个分段，浏览器持续调用到完成。关闭页面可中断后续批次，再次点击续传。上游失败不影响文档和关键词索引。
- 默认空知识库，没有虚构业务文档。单文档最多 10 万字符，单知识库最多 10000 分段；适合小规模私有知识库，向量为线性扫描，未实现 ANN、ES、PDF/Word 解析、多人角色、版本历史。
- 模型服务需配置公网 HTTPS 地址。保存地址和模型名后，可测试连接，再回到知识库生成向量。密钥服务端保存，读接口不返回。变更服务地址不会复用旧地址的密钥。
- 召回内容视作不可信参考数据，应由调用方控制系统指令并引用结果来源。BM25、cosine 和 RRF 的分数不可相互比较。

参考：[SQLite FTS5 / BM25](https://www.sqlite.org/fts5.html#the_bm25_function)、[Embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。

## 本地运行

```sh
export KB_ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export KB_READ_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export KB_DATA_DIR=/tmp/sweet-knowledge-local
python3 services/knowledge/server.py
```

访问 http://127.0.0.1:8765/knowledge/ ，用 `KB_ADMIN_TOKEN` 登录。浏览器密钥仅保存在内存，刷新后需登录。

```sh
python3 -m unittest discover -s services/knowledge -v
node --check knowledge/app.js
```

向量测试使用受控模拟接口验证格式、排序、模型变化与混合召回；真实语义质量和服务可用性需配置用户的模型后验证。

## 生产布局

- `/opt/sweet-knowledge/server.py` 与 `static/`：程序与页面。
- `/var/lib/sweet-knowledge/knowledge.db`：数据库、索引及模型配置，权限 0700 的服务数据目录。
- `/etc/sweet-knowledge.env`：管理密钥、只读召回密钥及路径，root 0600。
- `sweet-knowledge.service`：独立低权限用户运行，仅监听 `127.0.0.1:8765`。
- Nginx 仅在主站 HTTPS server 内代理 `/knowledge/`，不改其他页面。

环境文件格式（实际密钥不入库）：

```ini
KB_ADMIN_TOKEN=<独立随机管理密钥，至少24字符>
KB_READ_TOKEN=<独立随机只读密钥，至少24字符>
KB_DATA_DIR=/var/lib/sweet-knowledge
KB_STATIC_DIR=/opt/sweet-knowledge/static
KB_PORT=8765
```

Nginx 的 http 作用域：

```nginx
limit_req_zone $binary_remote_addr zone=knowledge_api:10m rate=5r/s;
```

主域名 HTTPS server 中：

```nginx
location = /knowledge { return 301 /knowledge/; }
location ^~ /knowledge/ {
    limit_req zone=knowledge_api burst=30 nodelay;
    limit_req_status 429;
    client_max_body_size 1m;
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_read_timeout 90s;
}
```

服务运维：`systemctl status sweet-knowledge`、`journalctl -u sweet-knowledge`。更新时只复制程序和静态资源，再重启服务；不要覆盖数据库和环境文件。Nginx 变更必须先备份并 `nginx -t` 后 reload。

## 接口

全部接口需 `Authorization: Bearer <token>`，POST/PUT 为 JSON。

| 方法与路径（前缀 `/knowledge/api`） | 用途 |
| --- | --- |
| GET /bases、POST /bases | 列出、创建知识库 |
| GET /bases/:id、PUT /bases/:id、DELETE /bases/:id | 读、改、删知识库 |
| GET /bases/:id/documents?q=、POST /bases/:id/documents | 文档列表 / 搜索、添加 |
| GET /documents/:id、PUT /documents/:id、DELETE /documents/:id | 读取原文和分段、修改、删除 |
| GET /settings、PUT /settings | 模型设置（不返回密钥） |
| POST /settings/test | 用已保存模型执行一次测试请求 |
| POST /bases/:id/embed | 为待处理分段生成一批向量 |
| POST /retrieve | 给大模型的召回入口，支持只读密钥 |

知识库写入：`name`、`description`、`chunk_size`（默认600）、`overlap`（默认80且小于长度一半）、`top_k`（默认5）。文档写入：`title`、`content`、`source`。PUT 为完整替换可编辑字段。

```sh
curl https://sweet-sleep.cn/knowledge/api/retrieve \
  -H "Authorization: Bearer $KB_READ_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"kb_id":"知识库ID","query":"如何申请退款","mode":"keyword","top_k":5,"max_context_chars":12000}'
```

响应 `results` 包含 `document_id`、`chunk_id`、`ordinal`（从0开始）、`title`、`source`、`content`、`score`、`citation`、`truncated`。`context` 是按字符预算截断并带引用标记的上下文。未命中返回空数组和空上下文；缺模型或未完成向量索引返回409，绝不把关键词结果冒充向量召回。只读密钥不可列出或修改知识库；管理者需要将具体知识库ID提供给调用方。

## 备份与恢复

使用 SQLite backup API 备份正在运行的数据库，不能仅复制 WAL 模式下的 `.db` 文件。备份中含模型 API Key，必须存放在私有目录。恢复时先停服务，再使用已验证备份替换数据库，恢复服务用户权限，然后启动服务。环境文件需要另行私密备份；服务密钥轮换后需重启服务。

## DeepSeek 客服回答

`/knowledge/` →「模型设置」新增独立的客服配置：启用开关、API Key、模型名（默认 `deepseek-v4-flash`）、System Prompt、按群的人工联系人，以及不发 QQ 消息的回答预览。Embedding 配置独立保存，修改客服模型不会清空向量。客服配置保存在 SQLite `app_settings` 中，读取不返回 API Key；空密钥保留，勾选清除后移除。配置每请求读取，保存立即生效。

- `GET/PUT /knowledge/api/answer-settings`：仅管理密钥可读写；PUT 字段 `enabled`、`model`、`api_key`、`clear_key`、`system_prompt`、`handoff_groups`（群 OpenID 到最多3个成员 OpenID 的映射）。
- `POST /knowledge/api/answer-settings/test`：管理者用已保存配置发送一条测试请求，会产生服务商调用费用。
- `POST /knowledge/api/answer`：管理密钥或召回密钥可调用，参数 `kb_id`、`query`、可选 `group_id`。配置了模型后，此接口可产生调用费用。机器人不能修改提示词或模型配置。

响应字段 `answer`（可展示文本）、`mode`（model/document/handoff）、`reason`（机器可读状态）、`handoff`、`mention_openids`（仅群聊转人工且配置匹配时返回）、`results`。密钥和上游完整错误不返回；后台可查看最近一次模型或回退状态。

回答流程：模型先生成2–5个不重复的检索词（JSON、最多300输出token），每词 BM25 Top 5 检索，再按 RRF 合并排序，对重复分段ID及相同文本去重，保留最多8个分段/6000字符。然后再次调用同一模型根据原问题和去重资料直接输出客服回复（普通文本、最多1000输出token），不要求引用、证据摘录或逐字校验，也不追加参考资料列表。两次调用均为非思考模式。资料不足时用内部转人工标记处理，不把标记显示给用户。

无检索命中时不执行回答阶段。没有密钥或关闭模型时，直接用原问题检索并返回最相关片段；关键词生成失败（包括余额不足/密钥失效）时同样回退原问题检索。回答阶段失败则返回合并结果中最相关片段。片段最长1000字符并附来源，错误详情不发给QQ用户。最多4条并发模型流水线。

管理员QQ默认471718054，用于展示联系信息；其QQ号不能当作群OpenID。真实艾特仍由后台按群配置成员OpenID。管理员在相应群里@机器人发送 `/身份` 后即可获取绑定所需信息，不会自动获得权限。

API响应增加 `search_terms`，后台回复预览显示本次检索词。`answer-settings` 增加可编辑 `admin_qq` 字段。测试覆盖检索词范围/去重、片段去重、直接回复、模型故障回退和群消息入口限制。真正的模型回答和群内提及以配置后的联调结果为准。

参考：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。
