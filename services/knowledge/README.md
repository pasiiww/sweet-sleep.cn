# 知屿 · 知识库管理与召回

独立页面 `/knowledge/`，Python 3.11+ 标准库后端。生产数据和凭据位于网站根目录之外；不得把整个仓库复制到公开 web root。

## 能力与边界

- 多知识库及文档增删改查；TXT / Markdown 导入；按字符长度与重叠长度切分。
- 关键词：SQLite FTS5 + BM25，中文字符 / 双字分词，英文词。这里没有安装或连接 Elasticsearch。
- 向量：OpenAI 兼容 Embeddings API；分段向量持久化到 SQLite，查询时精确余弦检索。
- 混合：两路候选以 RRF（k=60）融合；Top K 1–20，上下文预算 100–40000 字符。
- 文档、分段配置或模型更新后旧向量失效；手动生成向量，每请求处理最多 32 个分段，浏览器持续调用到完成。关闭页面可中断后续批次，再次点击续传。上游失败不影响文档和关键词索引。
- 默认空知识库，没有虚构业务文档。单文档最多 10 万字符，单知识库最多 10000 分段；适合小规模私有知识库，向量为线性扫描，未实现 ANN、ES、PDF/Word 解析、多人角色、版本历史或大模型生成回答。
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
