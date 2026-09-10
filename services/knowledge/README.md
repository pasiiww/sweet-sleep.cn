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

`/knowledge/` →「模型设置」新增独立的客服配置：启用开关、API Key、模型名（默认 `deepseek-flash`）、PE1 检索词提示词（`keyword_prompt`）、PE2 System Prompt（`system_prompt`）、按群的人工联系人，以及不发 QQ 消息的回答预览。Embedding 配置独立保存，修改客服模型不会清空向量。客服配置保存在 SQLite `app_settings` 中，读取不返回 API Key；空密钥保留，勾选清除后移除。配置每请求读取，保存立即生效。

- `GET/PUT /knowledge/api/answer-settings`：仅管理密钥可读写；PUT 字段 `enabled`、`model`、`api_key`、`clear_key`、`system_prompt`、`handoff_groups`（群 OpenID 到最多3个成员 OpenID 的映射）。
- `POST /knowledge/api/answer-settings/test`：管理者用已保存配置发送一条测试请求，会产生服务商调用费用。
- `POST /knowledge/api/answer`：管理密钥或召回密钥可调用，参数 `kb_id`、`query`、可选 `group_id`。配置了模型后，此接口可产生调用费用。机器人不能修改提示词或模型配置。

响应字段 `answer`（可展示文本）、`mode`（model/document/handoff）、`reason`（机器可读状态）、`handoff`、`mention_openids`（仅群聊转人工且配置匹配时返回）、`results`。密钥和上游完整错误不返回；后台可查看最近一次模型或回退状态。

回答流程：模型先生成2–5组不重复的关键词（JSON `query_groups`、最多1200输出token），每组1–4词；组内 AND、组间 OR，每组 BM25 Top 5 检索，再按 RRF 合并排序，对重复分段ID及相同文本去重，保留最多8个分段/6000字符。然后再次调用同一模型根据原问题和去重资料直接输出客服回复（普通文本、最多1000输出token），不要求引用、证据摘录或逐字校验，也不追加参考资料列表。两次调用均为非思考模式。资料不足时模型先组织回复，再追加内部转人工标记；程序保留模型回复、移除标记并决定可信艾特对象。只有模型未生成正文时才使用固定兜底转人工文案。回复语言和风格遵循后台 PE2 配置。

PE1 仅接收当前问题与当前命中的别名说明，不携带历史问答。PE1 输出异常时用当前问题的本地检索作为替代，仍执行 PE2；密钥失效、余额不足、无权限则直接回退原文。无检索命中也执行 PE2，由模型按 System Prompt 组织转人工回复；身份介绍与问候可以依据角色设定回答。没有密钥或关闭模型时，使用本地检索并返回最相关片段。回答阶段失败则返回合并结果中最相关片段。片段最长1000字符，直接回复原文而不附来源，错误详情不发给QQ用户。最多4条并发模型流水线。

管理员QQ默认471718054，作为后台备用信息；聊天显示管理员称呼（默认落落）。QQ号不能当作群OpenID。真实艾特仍由后台按群配置成员OpenID。管理员在相应群里@机器人发送 `/身份` 后即可获取绑定所需信息，不会自动获得权限。

API响应包含 `search_terms` 和 `query_groups`，后台回复预览以 `[凯伊 + 价格] / [kei + 定金]` 显示分组。无模型时 `query_groups` 为空，`search_terms` 为原问题。`answer-settings` 增加可编辑 `admin_qq` 字段。测试覆盖检索词范围/去重、片段去重、直接回复、模型故障回退和群消息入口限制。真正的模型回答和群内提及以配置后的联调结果为准。

参考：[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。

### 分组关键词召回

`POST /retrieve` 支持 `{"kb_id":"知识库ID","query_groups":[["凯伊","价格"],["kei","定金"]],"mode":"keyword","top_k":5}`。提供分组后 `query` 可省略；同时传入时分组决定匹配条件。后台「召回测试」也可展开分组输入直接测试，无需调用模型。

每组1–4词、每词1–80字符、最多5组；忽略大小写及组内顺序去重。FTS 缩小候选范围，然后逐词检查标题或当前分段是否含完整文本，避免中文拆字误命中。每组必须所有词匹配，不会在无结果时自动去掉角色名改搜所有价格。组间按 RRF 融合排名，同分时按分段ID稳定排序。无结果返回空列表。无需重建已有索引。

模型提示词要求每组保留实体标准名，优先原始意图；回答提示词要求区分总价、定金和尾款。分组只支持 keyword 模式，vector/hybrid 继续使用原来的 query 接口，组合传入返回400，避免默默放宽匹配。关键词依赖资料中的实际写法，别名和不同字段应放在不同组；跨分段的实体信息建议写在文档标题中。

### 实体别名与多轮上下文

后台「实体别名」按知识库以表格维护名称表，每行分别编辑标准名和别名（逗号或换行分隔，名称内部可含空格）。支持新增、搜索和逐行保存、撤销、删除；保存单行会保留其他行的未保存修改，并检查远端冲突。API 可传 `expected_items` 作并发比较，数据已变更时返回409。校验名称归属冲突；最多200实体、每实体20别名、每名称80字符。`GET/PUT /bases/{id}/entities` 仅管理员可访问，PUT 格式 `{"items":[{"name":"凯伊","aliases":["kei"]}]}`，空列表清空。随知识库删除，独立于业务文档和 Embedding 设置。

程序以最长名称优先、英文词边界和忽略大小写规则匹配问题及历史对话；确定命中的别名关系后，将“xxx 是 xxx 的别名”说明强制插入系统上下文：PE1 只使用当前问题的匹配，PE2 使用当前问题与历史的匹配。生成器只输出标准名，程序再次标准化生成结果。分组关键词检索在数据库层展开管理员配置的别名，兼容原文仅写旧别名的文档，保留原文供回答，无需重建索引。未配置的别名不凭模型猜测对应关系。

`POST /answer` 可携带 `history`，是最多20轮、总计24000字符的完整 user/assistant 消息对，禁止 system 消息，按时间顺序放在本次提问前。PE1 检索规划与 PE2 回答阶段收到同一份历史，历史仅用于指代与交流，不作为店铺事实依据。响应增加 `alias_context`、`matched_aliases` 和 `history_turns`，后台预览展示这些信息。该 API 不存储会话，30分钟期限由 QQ 客户端/后台预览维护；外部调用者负责历史选择。

QQ 客户端以知识库、私聊/群聊类型、群和用户 OpenID 的哈希组合隔离会话，SQLite 保存成功发送的问答对；读取时排除距当前超过30分钟的记录，删除过期数据并保留最近20轮/24000字符。同一会话串行处理直至发送成功，保证下一轮看到上一轮回复，重启后历史仍有效。身份缺失时不维护历史，普通群消息不进入历史。`/新对话` 或 `/清空上下文` 清除当前会话；群内仍须艾特机器人。

后台客服预览也按知识库维护当前页面的30分钟历史，并提供「清空预览对话」；刷新或退出后清空，与QQ历史隔离。

明确追问可能只生成1组有效关键词，程序接受，不为凑数使回答失败。模型不可用时，已有别名实体仍用于限定检索；“那定金呢”等明确字段追问可从历史用户问题恢复实体，避免退化成全库定金查询。


### 最近7天对话 Trace

从上线开始，为每次 `/answer` 保存 trace；后台「对话 Trace」支持时间、知识库、来源、回答状态、用户/群 OpenID 和全文关键词筛选，每页30条。列表直接展示问题、检索词、检索次数、回复、耗时、发送状态；详情包含 PE1/PE2 实际输入消息（含系统提示词、资料和各自携带的历史）、模型原始文本输出（每次最多4000字符）、每组命中排名、去重后的分段快照、别名说明、历史问答和本次提示词。转人工也保留曾召回的资料。API Key、鉴权头和上游错误正文不写入；已配置的模型密钥与后台访问密钥在内容中脱敏。

`GET /traces` 与 `GET /traces/{id}` 仅管理员可查询。筛选参数 `q,kb_id,origin,mode,user_id,group_id,session_id,start,end,offset`，start/end 为 Unix 秒。七天窗口按服务端时间强制限制；启动及每小时清理过期记录，删除知识库会同时删除其 trace。旧对话未记录的检索过程不回填。

`/answer` 可附带 `origin`（api/preview/qq_group/qq_private）、user_id、session_id，返回 trace_id；QQ 来源额外返回一次 trace 的保密回执 trace_receipt。机器人在实际发送后通过 `POST /trace-delivery` 回报 trace_id、receipt、status（delivered/failed）、content、error。召回密钥可提交具有正确回执的发送状态，但不可读取历史记录。回执不出现在查询结果。pending 表示未收到发送回执，不能据此认定消息已发送；模型回复和最终QQ发送内容分别展示。

QQ 帮助、身份、清空上下文等本地命令及未艾特的群消息不进入知识库处理，也不生成 trace。知识库服务不可达时无法生成服务端 trace，仍可查 QQ 服务错误日志。用于续聊的30分钟历史与调试用7天 trace 独立。


### QA 知识库

后台「QA 知识库」在所选知识库内独立维护 Q/A 条目，支持分页、按 Q 搜索、逐行新增/修改/删除/撤销。每库最多1000条，Q 最多1000字符，A 最多10000字符。`GET/POST /bases/{id}/qa` 与 `GET/PUT/DELETE /qa/{id}` 仅管理员可访问；更新、删除可携带 revision 防止覆盖他人修改。GET 列表支持 q（仅匹配问题）和 offset，每页20条。

QA 的 FTS 索引严格只写 Q，A 从不参与匹配。关键词召回同时查文档与 QA，使用相同实体标准化和分组 AND/OR 条件，然后按来源各自排名用 RRF 合并，避免比较不同索引的原始 BM25 分数。混合模式中 QA 仍只用关键词匹配 Q；纯 vector 模式仍仅检索文档。QA 无需配置向量或重建文档索引。

返回条目 source_type 为 document 或 qa。QA 的 content 是 A（单条最多2000字符，超长有 truncated 标记），question/title 是 Q，qa_id 是其ID，chunk_id/document_id 使用 qa: 前缀避免与文档分段冲突。Q+A 一起提供给模型说明适用问题，但只有Q被索引。文档和 QA 按共享预算合并，QA 去重同时考虑问题和答案，防止不同问题的相同短答案被错误合并。Trace 展示来源类型和对应参考原文。

回答模型分别接收 retrieved_documents 和 retrieved_qa，依据资料回答，冲突或不足则转人工。聊天回复不标注来源、引用或标题；模型失效时仍直接返回最相关文档原文或 QA 的 A。未找到答案时默认回复：“呜，这个问题我还不太确定呢～可以找管理员落落帮忙确认一下呀 ♡”。后台管理员称呼 admin_name 默认落落，可编辑；QQ/OpenID 仍供内部接管配置，回复不展示QQ号码。

## 持续学习与知识更新 Trace

后台「持续学习」按知识库配置启用开关、普通消息阈值（默认3条）、学习提示词和 `QQ号 / 群OpenID / 成员OpenID` 绑定，仅接受 `1229837719`、`471718054` 对应的已绑定成员作为知识来源。默认无绑定，不会自动入库。管理员在目标群 @机器人发送 `/身份` 后，由后台填写绑定。

- 管理员带引用的回复单条触发；普通发言同群合计达到阈值触发，或同群管理员连续10分钟没有发言时，将剩余普通发言提交学习。其他成员的发言不重置静默计时。
- 每条来源读取此前10条其他成员的已接收消息，合并后按消息 ID 去重。仅有机器人加入群且获得接收权限后的记录可用，未收到的历史不凭空补全。引用正文与本次管理员原话分开传入模型；引用仅帮助理解，不能单独作为已确认事实。
- 一次模型调用完成相关性判断和事实提取。闲聊、猜测、无依据信息不入库。自动知识整理为独立 QA，使用实体、属性、适用范围识别已有自动知识；相同范围的较新消息更新旧值，较旧消息或处理期间被人工修改的记录跳过。
- QA 更新时间使用来源消息时间，文档和 QA 的 `updated_at` 都送入 PE2；仅同一实体、属性及条件的冲突采用较新的资料。文档本身保留，不因一条价格变动删除整份资料。
- 学习 Trace 包含触发方式、每条来源的前置消息索引、去重上下文、引用内容、相关性理由、旧知识检索、模型输入/输出、写入前后值及跳过原因，保留7天。失败任务最多自动尝试3次，可在后台查看并重试；配置变化会取消旧配置的未完成任务。

使用独立的 `KB_LEARN_TOKEN`：学习密钥仅允许 `POST /knowledge/api/learning/events`，不能读取或编辑后台配置；原 `KB_READ_TOKEN` 仍不能提交学习消息。学习事件先写入 SQLite，再由独立后台线程处理，重启后恢复未完成任务，不占用客服回复流水线。QQ 进程也有持久化上传队列，失败重发时以群和消息 ID 去重。

管理接口：`GET/PUT /bases/:id/learning`、`GET /learning/jobs?kb_id=...`、`GET /learning/jobs/:id`、`POST /learning/jobs/:id/retry`。学习 Trace 位于「持续学习」页面下方。

回归测试运行 `python3 services/knowledge/run_tests.py`。此运行器禁止访问外部 API，只允许本机 HTTP 测试服务；模型结果由 mock 提供。真实模型验收单独在临时知识库中进行，不放入回归测试。

## 表情包库

后台「表情包库」全局维护名称、图片路径、启用状态，支持逐行编辑与删除，最多 100 条。图片必须已存在，可填写公开 HTTPS PNG/JPG/GIF 地址、本站 `/stickers/开心.png` 或 `/www/wwwroot/myweb/` 下的路径；也可直接选择 PNG/JPG/GIF 上传（单张最大 5 MB），保存到 `/var/lib/sweet-knowledge/stickers/`，自动生成随机文件名与 `/knowledge/sticker-files/` 公开图片地址。上传需管理密钥，图片可供 QQ 拉取；删除库条目保留文件，避免在途消息失效。目录总容量 250 MB。

PE2 只接收启用名称列表，提示词列出 `[名称]` 表情上下文，模型以 `[玲纱-开心]` 等精确名称选择 0 或 1 张（兼容旧标记），服务端校验后移除标记并返回 `sticker` 描述。群聊与私聊通过 QQ 文件上传接口（`file_type=1, srv_send_msg=false`）取得 `file_info`，再用 `msg_type=7` 随文字被动回复。图片上传或发送失败会尝试保留原文字回复；Trace 记录选图与发送状态，不保存临时 `file_info`。模型不可用时原文回退不附图。

上传或添加时名称可留空，使用现有回答 API Key 和后台模型配置（默认 `deepseek-flash`）看图命名；手动名称不调用模型。同名自动追加序号，失败提示手动命名或重试。GIF 保留动画原文件，以 `image/gif` 提供下载并交给 QQ 富媒体接口，QQ 拒绝图片时仍保留文字回复。

PE1 支持 `{"query_groups":[]}` 表示无需检索：问候、感谢和没有具体咨询内容的开场白（例如“你知道吗”）不会被历史商品或 QA 示例扩展成查询。空计划跳过原文召回与空召回重试，PE2 收到 `retrieval_skipped=true` 后自然接话；有明确意图的“多少钱”“定金呢”仍结合历史补全实体。Trace 标记跳过原因。

表情包 context 仅在每次 PE2 请求的 system prompt 中提供一次，不在当前消息中重复列表，也不随历史轮次累积。频率建议：闲聊类可以较高频率使用表情包，也可以只回复表情包；店铺咨询类控制在50%的轮次以下。代码不按频率拦截；保留上一条实际发送状态作为模型参考，名称校验及每次最多一张的映射保持不变。Trace 展示建议规则。

## Owner 更新通知

回答提示词不再注入管理员姓名或 QQ，资料不足统一建议联系群主或管理员，不再自动艾特旧接管配置。学习管理员绑定仍用于权限校验，不受影响。

「持续学习 → 知识更新 · 通知 bot owner」配置 owner（471718054）的私聊 OpenID 并启用。Owner 私聊机器人发送 `/身份` 可查看自己的 C2C OpenID；QQ 号与群成员 OpenID 不可替代。通知需要 QQ 允许该机器人向收件人主动发送私信，平台拒收显示失败。尚未绑定时不自动猜测收件人、不发送。

文档新增/内容修改、QA 生效/更新在同一数据库事务中写通知队列；待审核不通知，审核生效后通知；回滚不产生通知。同知识库最多8条变更合成摘要。机器人用 QQ C2C 主动消息发送，不伪造 msg_id。结果写入通知状态；失败或结果未知不自动重复发送，可在后台人工重试。通知最多保留7天。Owner OpenID 和通知内容不进入回答模型上下文。

2026-09-10 实测 `deepseek-flash` 支持文本 JSON 和图片理解。PE1、PE2、持续学习、表情包命名统一读取后台回答模型配置，不再固定使用独立视觉模型。

持续学习的上下文窗口、双方成员选择、条数限制和去重由 `enqueue()` 的数据库查询完成。模型只接收组装后的 `context`、`context_by_source` 和本批来源消息 ID。双方窗口/条数索引保留在学习 Trace，不传给模型。学习提示词删除取数算法说明；旧保存配置及待处理任务在使用时清理这段描述，用户其他提示词保留，默认学习提示词不重复拼接。

闲聊支持仅回复一个表情包标记，解析后可返回空文字与一个已验证图片描述；QQ 以 `msg_type=7` 且不附文字发送。图片失败时提供简短文字回退，不发送空文本消息。已发送图片的名称写入多轮历史作为实际发送记录，避免纯图片导致历史校验失败；后台预览与 Trace 可显示“仅表情包”。咨询问题仍以文字解答，50%频率只是提示词建议。
# 学习输入清洗

学习模型调用前过滤空白、纯表情编码、纯笑声/标点刷屏和无新增引用或提及关系的相邻重复回复。保留管理员对普通成员的同句确认，保留有含义的短回复、金额、日期、商品名和链接。查重检索同步使用清洗文本。

模型上下文使用 `m1` 消息编号、`u1` 成员编号及 `trusted` 身份标记，引用和上下文索引保持关联。模型返回的来源编号在原文校验与写入前还原。完整原文仍保存在 Trace 和知识来源中；学习 Trace 的 `context_preprocessing` 记录清洗前后数量、过滤原因和编号映射。纯噪声批次不调用模型。
# 社团制品管理

一条记录代表一个系列商品/平台商品，可填写多个角色。类型维护通用参考价，`variants` 单独维护“角色 × 类型”的价格与售卖情况（待确认、在售、售罄、不售卖）。未配置组合不推断有货，也不根据平台起步价、优惠价推算具体规格价格。旧版单角色记录自动兼容，保存时使用新的角色列表。

管理后台的“社团制品”页按知识库维护制品：系列名（可空）、角色名、图片、多个制品类型及各自参考价、多个平台链接、备注。支持搜索、新增、编辑和带版本检查的删除。图片上传不调用模型，也不加入表情包库。

“供机器人检索”默认关闭。开启后在同一事务内维护一份对应知识库文档，内容明确标注价格仅供参考、具体以平台为准；更新时重建分段，关闭或删除时移除同步文档。管理 API `/products`、`/products/{id}`、`/products/upload` 仅管理密钥可用。

回答历史清洗仅移除 QQ 表情编码、提及标签和空问答对，保留自然表情、短确认和事实细节；旧表情包发送记录规范成 `[名称]`。引用原文通过 `reply_reference`（最多1800字符）送入两阶段当前消息，并在发送成功后随用户问题保存。平台未提供引用正文时不编造。Trace 保留清洗后的历史、引用内容与20轮/24000字符策略。

DeepSeek 统一调用层启用 `thinking.type=enabled`、`reasoning_effort=low`，包括检索规划、回答、学习和表情命名。思考与最终输出的合计预算至少8192 tokens；仅最终 `content` 用于业务回复及后续历史，不记录/转发 `reasoning_content`。Trace 记录思考开关、强度、预算与实际 usage。

### 私聊维护工具

私聊 `/help` 查看指令，`/modify 知识库 修改要求`、`/modify qa 修改要求`、`/add 商品库 商品信息` 开始维护。后续自然语言沿用所选库，最近30分钟最多6轮维护历史与顾客咨询隔离；`/退出` 结束。群聊不开放写操作，维护不占顾客20次咨询额度。

后台「模型设置 → 私聊维护权限」配置独立私聊 OpenID 白名单，通知收件人不自动获得权限。`GET/PUT /private-maintenance-settings` 仅管理密钥可访问，`POST /private-maintenance` 仅服务端学习密钥或管理密钥可访问；普通召回密钥不可调用。QQ 端传入平台真实 user_openid、消息ID和固定知识库ID。

原生 function calling 提供 search_records、read_record、update_record、add_product，按指令限定工具；最多4次模型调用、每次一个工具、每条消息最多写一条。修改必须先读取同库原文并核对摘要以避免并发覆盖。新增商品复用后台字段验证。缺字段或目标不明确时追问；未知价格、链接、图片不猜测，商品默认不用于召回。暂不接收私聊图片附件，图片可提供URL或后续后台上传。

同一消息7天内幂等，写入结果与数据变更在同一事务提交。对话 Trace 的「私聊维护操作」显示工具、参数、修改前后内容；QA 的 updated_by 标为 private_admin_ai，原始指令保留在 trace。thinking 工具循环按协议在内存回传 reasoning_content，不存入 trace、历史或发送给 QQ。
