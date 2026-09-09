'use strict';
const $ = id => document.getElementById(id);
const state = { token: '', bases: [], selected: '', editBase: null, editDoc: null, context: '', settings: {}, embedding: false };
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
let toastTimer;
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5500); }
async function api(path, method = 'GET', body) {
  const response = await fetch('/knowledge/api/' + path, { method, headers: { Authorization: 'Bearer ' + state.token, ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined });
  let data;
  try { data = await response.json(); } catch { throw new Error('服务器响应异常，请稍后重试'); }
  if (!response.ok) { if (response.status === 401) logout(); throw new Error(data.error || '请求失败'); }
  return data;
}
async function busy(button, action) {
  if (button.disabled) return;
  const previous = button.textContent; button.disabled = true; button.textContent = '处理中…';
  try { await action(); } catch (error) { toast(error.message); }
  finally { button.disabled = false; button.textContent = previous; }
}
function logout() {
  previewTurns.clear();
  state.token = ''; state.bases = []; state.selected = ''; state.context = '';
  $('workspace').hidden = true; $('login').hidden = false; $('token').value = '';
  document.querySelectorAll('dialog[open]').forEach(d => d.close());
  $('embedding-key').value = ''; $('answer-key').value = ''; $('doc-content').value = '';
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('login-error').textContent = '';
  await busy(event.submitter, async () => {
    state.token = $('token').value.trim();
    try { await refresh(); await loadSettings(); await loadAnswerSettings(); $('login').hidden = true; $('workspace').hidden = false; $('token').value = ''; }
    catch (error) { $('login-error').textContent = error.message; state.token = ''; }
  });
});
$('logout').onclick = logout;
const views = { traces: ['对话 Trace', '从问题到回复，查看每次检索与模型调用的过程。'], aliases: ['实体别名', '统一名称，让不同称呼都能找到同一份知识。'], documents: ['知识库', '把分散的信息，变成有据可依的回答。'], retrieve: ['召回测试', '在连接大模型之前，先看看知识是否被准确找到。'], integration: ['API 接入', '把你的知识，接入任意大模型工作流。'], settings: ['模型设置', '为你的知识库，连接语义理解能力。'] };
document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => {
  const view = button.dataset.view;
  document.querySelectorAll('.view').forEach(el => el.hidden = el.id !== 'view-' + view);
  document.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('active', el === button));
  $('page-title').textContent = $('breadcrumb').textContent = views[view][0]; $('page-subtitle').textContent = views[view][1];
  $('create-base').hidden = view !== 'documents';
  if (view === 'traces') loadTraces(true).catch(error => toast(error.message));
});
async function refresh() {
  const { items } = await api('bases'); state.bases = items;
  if (!items.some(b => b.id === state.selected)) state.selected = items[0]?.id || '';
  $('base-count').textContent = items.length;
  $('stats').innerHTML = [ ['知识库', items.length, '▤'], ['文档总数', items.reduce((s, b) => s + b.document_count, 0), '▧'], ['可检索分段', items.reduce((s, b) => s + b.chunk_count, 0), '⌘'] ].map(([title, count, icon]) => `<div class="stat"><div><small>${title}</small><strong>${count.toLocaleString()}</strong></div><div class="stat-icon">${icon}</div></div>`).join('');
  $('base-list').innerHTML = items.map(b => `<button class="base-card ${b.id === state.selected ? 'selected' : ''}" data-base="${b.id}"><div class="base-card-top"><span class="base-icon">▤</span><span class="badge ${b.vector_count < b.chunk_count ? 'pending' : ''}">${b.chunk_count && b.vector_count === b.chunk_count ? '向量已就绪' : '关键词检索'}</span></div><h3>${esc(b.name)}</h3><p>${esc(b.description || '为大模型准备有来源的知识')}</p><div class="base-card-bottom"><span>${b.document_count} 份文档</span><span>${b.chunk_count} 个分段</span><span>${b.vector_count} 个向量</span></div></button>`).join('');
  $('no-base').hidden = items.length > 0; $('document-panel').hidden = !state.selected;
  for (const id of ['retrieve-base', 'api-base', 'answer-base', 'aliases-base']) {
    const previous = $(id).value;
    $(id).innerHTML = items.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
    $(id).value = items.some(b => b.id === previous) ? previous : state.selected;
  }
  const traceBase = $('trace-base').value;
  $('trace-base').innerHTML = '<option value="">全部知识库</option>' + items.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
  $('trace-base').value = items.some(b => b.id === traceBase) ? traceBase : '';
  updateExample();
  await loadAliases();
  if (state.selected) {
    const selected = items.find(b => b.id === state.selected);
    $('selected-base-name').textContent = selected.name; $('selected-base-description').textContent = selected.description || '管理文档内容与召回索引';
    await loadDocuments();
  }
}
$('base-list').onclick = event => { const card = event.target.closest('[data-base]'); if (card) { state.selected = card.dataset.base; $('doc-search').value = ''; refresh().catch(e => toast(e.message)); } };
let docRequest = 0;
async function loadDocuments() {
  if (!state.selected) return;
  const requestId = ++docRequest;
  const { items } = await api(`bases/${state.selected}/documents?q=${encodeURIComponent($('doc-search').value)}`);
  if (requestId !== docRequest) return;
  $('document-list').innerHTML = items.map(d => `<tr><td title="${esc(d.title)}"><span class="doc-symbol">▧</span>${esc(d.title)}</td><td>${d.chunk_count}</td><td>${d.chars.toLocaleString()}</td><td>${esc(new Date(d.updated_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }))}</td><td><button class="row-button" data-edit="${d.id}">查看 / 编辑</button><button class="row-button delete" data-delete="${d.id}" data-title="${esc(d.title)}">删除</button></td></tr>`).join('');
  $('no-documents').hidden = items.length > 0; $('no-documents').querySelector('h3').textContent = $('doc-search').value ? '没有匹配的文档' : '准备好添加第一份文档了';
  $('document-total').textContent = `共 ${items.length} 份文档`;
}
let searchTimer;
$('doc-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadDocuments().catch(e => toast(e.message)), 200); };
function openBase(edit) {
  const b = edit ? state.bases.find(b => b.id === state.selected) : null; state.editBase = b?.id || null;
  $('base-dialog-title').textContent = b ? '知识库设置' : '新建知识库'; $('base-name').value = b?.name || ''; $('base-description').value = b?.description || '';
  $('chunk-size').value = b?.chunk_size || 600; $('chunk-overlap').value = b?.overlap ?? 80; $('base-top-k').value = b?.top_k || 5;
  $('delete-base').hidden = !b; $('base-dialog').showModal();
}
$('create-base').onclick = $('first-base').onclick = () => openBase(false); $('edit-base').onclick = () => openBase(true);
document.querySelectorAll('.close-dialog').forEach(button => button.onclick = () => button.closest('dialog').close());
$('base-form').onsubmit = event => {
  event.preventDefault(); busy(event.submitter, async () => {
    const payload = { name: $('base-name').value, description: $('base-description').value, chunk_size: +$('chunk-size').value, overlap: +$('chunk-overlap').value, top_k: +$('base-top-k').value };
    const result = await api(state.editBase ? 'bases/' + state.editBase : 'bases', state.editBase ? 'PUT' : 'POST', payload);
    state.selected = result.id; $('base-dialog').close(); await refresh(); toast('知识库已保存');
  });
};
function confirmDelete(message) {
  return new Promise(resolve => { $('confirm-message').textContent = message; $('confirm-dialog').returnValue = 'cancel'; $('confirm-dialog').addEventListener('close', () => resolve($('confirm-dialog').returnValue === 'delete'), { once: true }); $('confirm-dialog').showModal(); });
}
$('delete-base').onclick = () => busy($('delete-base'), async () => {
  if (!await confirmDelete('将删除这个知识库及其全部文档、分段和向量，此操作不可撤销。')) return;
  await api('bases/' + state.editBase, 'DELETE'); $('base-dialog').close(); await refresh(); toast('知识库已删除');
});
function openDoc(document = {}) {
  state.editDoc = document.id || null; $('doc-dialog-title').textContent = document.id ? '查看 / 编辑文档' : '添加文档';
  $('doc-title').value = document.title || ''; $('doc-source').value = document.source || ''; $('doc-content').value = document.content || ''; $('doc-dialog').showModal();
}
$('create-doc').onclick = () => openDoc();
$('doc-form').onsubmit = event => { event.preventDefault(); busy(event.submitter, async () => {
  await api(state.editDoc ? 'documents/' + state.editDoc : `bases/${state.selected}/documents`, state.editDoc ? 'PUT' : 'POST', { title: $('doc-title').value, source: $('doc-source').value, content: $('doc-content').value });
  $('doc-dialog').close(); await refresh(); toast('文档已保存，关键词索引已更新');
}); };
$('document-list').onclick = async event => {
  const edit = event.target.closest('[data-edit]'), del = event.target.closest('[data-delete]');
  if (edit) await busy(edit, async () => openDoc(await api('documents/' + edit.dataset.edit)));
  if (del) await busy(del, async () => { if (await confirmDelete(`删除「${del.dataset.title}」？其分段和向量也会一起删除。`)) { await api('documents/' + del.dataset.delete, 'DELETE'); await refresh(); toast('文档已删除'); } });
};
$('import-doc').onclick = () => $('file-input').click();
$('file-input').onchange = async () => {
  const file = $('file-input').files[0]; $('file-input').value = ''; if (!file) return;
  try { if (!/\.(txt|md|markdown)$/i.test(file.name)) throw new Error('请选择 TXT 或 Markdown 文件'); if (file.size > 500000) throw new Error('文件过大，请控制在 500 KB 以内');
    const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
    if (content.length > 100000) throw new Error('正文超过 10 万字符，请拆分后导入');
    openDoc({ title: file.name.replace(/\.[^.]+$/, '').slice(0, 200), source: file.name, content });
  } catch (error) { toast(error instanceof TypeError ? '文件不是有效的 UTF-8 文本，请转换编码后导入' : error.message); }
};
$('embed-base').onclick = () => busy($('embed-base'), async () => {
  const kbId = state.selected;
  if (!state.settings.ready) throw new Error('请先在「模型设置」配置 Embedding 服务');
  if (!await confirmAction('生成向量将把当前知识库分段发送到你配置的 Embedding 服务，服务商可能按使用量收费。是否继续？')) return;
  let response;
  do { response = await api(`bases/${kbId}/embed`, 'POST', {}); $('embed-base').textContent = `生成中 · 剩余 ${response.remaining}`; } while (response.remaining > 0 && state.token);
  await refresh(); toast('向量生成完成');
});
function confirmAction(message) {
  $('confirm-title').textContent = '生成向量';
  const button = $('confirm-dialog').querySelector('[value="delete"]'); button.textContent = '开始生成';
  return confirmDelete(message).finally(() => { $('confirm-title').textContent = '确认删除'; button.textContent = '确认删除'; });
}
$('retrieve-base').onchange = () => { $('retrieve-top-k').value = state.bases.find(b => b.id === $('retrieve-base').value)?.top_k || 5; };
$('retrieve-form').onsubmit = event => { event.preventDefault(); busy(event.submitter, async () => {
  state.context = ''; $('context-actions').hidden = true; $('result-meta').textContent = '查询中'; $('results').innerHTML = '<div class="empty">正在检索知识库…</div>';
  try {
    const grouped = $('retrieve-groups').value.trim();
    let groups;
    if (grouped) {
      try { groups = JSON.parse(grouped); } catch { throw new Error('关键词组格式错误，例如 [["凯伊","价格"],["kei","定金"]]'); }
      if ($('retrieve-mode').value !== 'keyword') throw new Error('分组关键词请选择关键词检索模式');
    }
    const result = await api('retrieve', 'POST', { ...(grouped ? {query_groups: groups} : {}), kb_id: $('retrieve-base').value, query: $('retrieve-query').value, mode: $('retrieve-mode').value, top_k: +$('retrieve-top-k').value, max_context_chars: +$('context-budget').value });
    $('result-meta').textContent = `${result.results.length} 个片段 · ${result.elapsed_ms} ms`;
    $('results').innerHTML = result.results.length ? result.results.map(r => `<article class="result-card"><div class="result-title"><strong>[${r.citation}] ${esc(r.title)}</strong><span class="badge">${esc(result.score_type.toUpperCase())} ${Number(r.score).toPrecision(4)}</span></div><p>${esc(r.content)}</p><small>来源：${esc(r.source || r.title)} · 分段 ${r.ordinal + 1}${r.truncated ? ' · 已按上下文预算截断' : ''}</small></article>`).join('') : '<div class="empty"><h3>没有找到相关片段</h3><p>试试不同关键词，或为知识库补充相关内容。</p></div>';
    state.context = result.context; $('context-actions').hidden = !result.context;
  } catch (error) { $('result-meta').textContent = '查询未完成'; $('results').innerHTML = `<div class="empty"><p class="error">${esc(error.message)}</p></div>`; throw error; }
}); };
async function copy(text) { try { await navigator.clipboard.writeText(text); toast('已复制'); } catch { toast('浏览器不允许自动复制，请手动选中内容复制'); } }
$('copy-context').onclick = () => copy(state.context);
function updateExample() {
  const body = JSON.stringify({ kb_id: $('api-base').value || '<知识库 ID>', query: '如何申请退款？', mode: 'keyword', top_k: 5, max_context_chars: 12000 }, null, 2);
  $('api-example').textContent = `curl '${location.origin}/knowledge/api/retrieve' \\\n  -H "Authorization: Bearer $KB_READ_TOKEN" \\\n  -H 'Content-Type: application/json' \\\n  -d '${body}'`;
}
$('api-base').onchange = updateExample; $('copy-api').onclick = () => copy($('api-example').textContent);
async function loadSettings() {
  const cfg = await api('settings'); state.settings = cfg;
  $('embedding-url').value = cfg.base_url; $('embedding-model').value = cfg.model; $('embedding-key').value = ''; $('clear-key').checked = false;
  $('key-status').textContent = cfg.has_key ? '已保存密钥。留空可保留；更换服务地址时请重新填写。' : '密钥仅存放于服务器，不会返回浏览器。';
}
$('settings-form').onsubmit = event => { event.preventDefault(); busy(event.submitter, async () => {
  await api('settings', 'PUT', { base_url: $('embedding-url').value, model: $('embedding-model').value, api_key: $('embedding-key').value, clear_key: $('clear-key').checked });
  await loadSettings(); await refresh(); $('model-test-result').textContent = ''; toast('模型配置已保存，请为知识库重新生成向量');
}); };
$('test-model').onclick = () => busy($('test-model'), async () => { const result = await api('settings/test', 'POST', {}); $('model-test-result').textContent = `连接成功 · ${result.dimensions} 维向量`; });

const answerReasons = {ok:'模型工作正常',missing_key:'尚未填写 API Key，将返回最相关文档',disabled:'模型已关闭，将返回最相关文档',invalid_key:'API Key 无效，将返回最相关文档',insufficient_balance:'余额不足，将返回最相关文档',access_denied:'模型无访问权限，将返回最相关文档',rate_limited:'模型限流，已回退原文',network_error:'模型网络异常或超时，已回退原文',upstream_error:'模型服务异常，已回退原文',invalid_response:'模型响应异常，已回退原文',invalid_keywords:'检索词生成失败，已用原问题检索并回退原文',no_results:'没有检索命中，已转人工',insufficient_evidence:'资料不足以回答，已转人工',busy:'模型请求较多，已回退原文'};
let answerDefaults = '';
async function loadAnswerSettings() {
  const cfg = await api('answer-settings');
  answerDefaults = cfg.default_prompt;
  $('answer-enabled').checked = cfg.enabled;
  $('answer-model').value = cfg.model;
  $('admin-qq').value = cfg.admin_qq;
  $('answer-key').value = '';
  $('answer-clear-key').checked = false;
  $('answer-prompt').value = cfg.system_prompt;
  $('handoff-groups').value = Object.entries(cfg.handoff_groups).map(([group, ids]) => [group, ...ids].join(' ')).join('\n');
  $('answer-key-status').textContent = cfg.has_key ? '已保存密钥，留空保留原密钥。' : '尚未保存 DeepSeek 密钥。';
  $('answer-status').textContent = !cfg.enabled ? answerReasons.disabled : !cfg.has_key ? answerReasons.missing_key : cfg.last_status ? '最近状态：' + (answerReasons[cfg.last_status.reason] || cfg.last_status.reason) + ' · ' + new Date(cfg.last_status.at).toLocaleString('zh-CN') : '已配置，下一次提问将调用模型。';
}
$('restore-prompt').onclick = () => { $('answer-prompt').value = answerDefaults; toast('已恢复初版提示词，保存后生效'); };
$('answer-settings-form').onsubmit = event => { event.preventDefault(); busy(event.submitter, async () => {
  const groups = Object.create(null);
  for (const line of $('handoff-groups').value.split('\n').filter(line => line.trim())) {
    const [group, ...ids] = line.trim().split(/\s+/);
    if (Object.hasOwn(groups, group)) throw new Error('同一个群请写在同一行');
    groups[group] = ids;
  }
  await api('answer-settings', 'PUT', { enabled: $('answer-enabled').checked, model: $('answer-model').value, api_key: $('answer-key').value, clear_key: $('answer-clear-key').checked, system_prompt: $('answer-prompt').value, admin_qq: $('admin-qq').value, handoff_groups: groups });
  await loadAnswerSettings(); $('answer-test-status').textContent = ''; toast('客服配置已保存，下次提问立即生效');
}); };
$('test-answer-model').onclick = () => busy($('test-answer-model'), async () => {
  try { const result = await api('answer-settings/test', 'POST', {}); $('answer-test-status').textContent = `连接成功 · ${result.model}`; }
  catch (error) { $('answer-test-status').textContent = error.message.replace(/invalid_key|insufficient_balance|access_denied|rate_limited|network_error|upstream_error|invalid_response|invalid_keywords/g, key => answerReasons[key]); }
  const cfg = await api('answer-settings');
  if (cfg.last_status) $('answer-status').textContent = answerReasons[cfg.last_status.reason] || cfg.last_status.reason;
});
const previewTurns = new Map();
function previewHistory(kb) {
  const recent = (previewTurns.get(kb) || []).filter(turn => turn.at > Date.now() - 1800000).slice(-10);
  let size = 0;
  const selected = [];
  for (const turn of recent.reverse()) {
    if (size + turn.query.length + turn.reply.length > 12000) break;
    size += turn.query.length + turn.reply.length; selected.unshift(turn);
  }
  previewTurns.set(kb, selected);
  return selected.flatMap(turn => [{role:'user',content:turn.query},{role:'assistant',content:turn.reply}]);
}
$('clear-preview-history').onclick = () => { previewTurns.delete($('answer-base').value); $('answer-preview').textContent = '已清空当前知识库的预览对话。'; };
$('answer-preview-form').onsubmit = event => { event.preventDefault(); busy(event.submitter, async () => {
  $('answer-preview').textContent = '正在检索并生成回复…';
  try {
    const kb = $('answer-base').value, query = $('answer-query').value;
    const result = await api('answer', 'POST', {kb_id: kb, query, origin: 'preview', history: previewHistory(kb)});
    previewTurns.set(kb, [...(previewTurns.get(kb) || []), {query, reply: result.answer, at: Date.now()}].slice(-10));
    $('answer-preview').textContent = (answerReasons[result.reason] || result.mode) + (result.trace_id ? '\nTrace ID：' + result.trace_id : '') + '\n携带历史：' + result.history_turns + ' 轮' + (result.alias_context ? '\n' + result.alias_context : '') + '\n检索词：' + (result.search_terms || []).map(group => Array.isArray(group) ? '[' + group.join(' + ') + ']' : group).join(' / ') + '\n\n' + result.answer + (result.handoff ? '\n\n实际群聊会按该群的人工联系人配置尝试艾特；这里仅预览文本。' : '');
  } catch (error) { $('answer-preview').textContent = error.message; throw error; }
}); };

let aliasesRequest = 0, aliasSequence = 0, aliasSaving = false, aliasRows = [], aliasesKb = '';
const aliasValue = row => ({name: row.name.trim(), aliases: row.aliases.split(/[,，;；|\n]+/).map(value => value.trim()).filter(Boolean)});
const aliasDirty = row => !row.original || JSON.stringify(aliasValue(row)) !== JSON.stringify(row.original);
function renderAliases() {
  $('alias-rows').innerHTML = aliasRows.map(row => `<tr data-alias-row="${row.id}"><td><input data-field="name" aria-label="实体标准名 ${row.id}" maxlength="80" placeholder="例如：凯伊" value="${esc(row.name)}"></td><td><textarea data-field="aliases" aria-label="别名 ${row.id}" rows="2" maxlength="2000" placeholder="例如：kei，小凯">${esc(row.aliases)}</textarea></td><td><span class="badge" data-row-status></span></td><td><div class="alias-row-actions"><button data-alias-action="save" class="row-button">保存</button><button data-alias-action="reset" class="row-button">撤销</button><button data-alias-action="delete" class="row-button delete">删除</button></div></td></tr>`).join('');
  updateAliasRows();
}
function updateAliasRows() {
  const filter = $('alias-search').value.trim().toLocaleLowerCase();
  for (const tr of $('alias-rows').children) {
    const row = aliasRows.find(item => item.id === tr.dataset.aliasRow), dirty = aliasDirty(row);
    tr.hidden = !!filter && ![row.name, row.aliases].join(' ').toLocaleLowerCase().includes(filter);
    tr.classList.toggle('alias-dirty', dirty);
    const status = tr.querySelector('[data-row-status]');
    status.textContent = !row.original ? '新增' : dirty ? '未保存' : '已保存';
    status.classList.toggle('pending', dirty);
    tr.querySelector('[data-alias-action="save"]').disabled = aliasSaving || !dirty;
    tr.querySelector('[data-alias-action="reset"]').disabled = aliasSaving || !dirty;
    tr.querySelector('[data-alias-action="delete"]').disabled = aliasSaving;
    tr.querySelectorAll('input,textarea').forEach(input => { input.disabled = aliasSaving; });
  }
  $('aliases-base').disabled = aliasSaving;
  $('add-alias').disabled = aliasSaving || !aliasesKb || aliasRows.length >= 200;
  $('aliases-empty').hidden = aliasRows.length > 0;
  const changed = aliasRows.filter(aliasDirty).length;
  $('aliases-status').textContent = `共 ${aliasRows.length} 个实体${changed ? ` · ${changed} 行未保存` : ' · 修改后逐行保存'}`;
}
async function loadAliases() {
  const kb = $('aliases-base').value, request = ++aliasesRequest;
  aliasesKb = ''; aliasRows = []; $('alias-search').value = ''; renderAliases();
  $('aliases-status').textContent = kb ? '正在读取…' : '请先创建知识库';
  if (!kb) return;
  try {
    const result = await api(`bases/${kb}/entities`);
    if (request !== aliasesRequest) return;
    aliasesKb = kb;
    aliasRows = result.items.map(item => ({id: String(++aliasSequence), original: item, name: item.name, aliases: item.aliases.join('，')}));
    renderAliases();
  } catch (error) { if (request === aliasesRequest) $('aliases-status').textContent = error.message; throw error; }
}
$('aliases-base').onchange = () => loadAliases().catch(error => toast(error.message));
$('alias-search').oninput = updateAliasRows;
$('add-alias').onclick = () => {
  $('alias-search').value = '';
  aliasRows.push({id: String(++aliasSequence), original: null, name: '', aliases: ''});
  renderAliases(); $('alias-rows').lastElementChild.querySelector('input').focus();
};
$('alias-rows').oninput = event => {
  const field = event.target.dataset.field, tr = event.target.closest('[data-alias-row]');
  if (!field || !tr) return;
  aliasRows.find(row => row.id === tr.dataset.aliasRow)[field] = event.target.value;
  updateAliasRows();
};
$('alias-rows').onclick = async event => {
  const button = event.target.closest('[data-alias-action]');
  if (!button || button.disabled || aliasSaving) return;
  const action = button.dataset.aliasAction, row = aliasRows.find(item => item.id === button.closest('tr').dataset.aliasRow);
  if (action === 'reset') {
    if (row.original) { row.name = row.original.name; row.aliases = row.original.aliases.join('，'); }
    else aliasRows = aliasRows.filter(item => item !== row);
    renderAliases(); return;
  }
  const kb = aliasesKb;
  if (action === 'delete' && row.original && !await confirmDelete(`删除实体「${row.original.name}」及其全部别名？`)) return;
  if (kb !== aliasesKb || !aliasRows.includes(row)) return;
  if (action === 'delete' && !row.original) { aliasRows = aliasRows.filter(item => item !== row); renderAliases(); return; }
  const value = aliasValue(row);
  if (action === 'save' && !value.name) { toast('请填写实体标准名'); return; }
  aliasSaving = true; updateAliasRows(); button.textContent = '处理中…';
  try {
    // Merge only this row with the latest catalog; other unsaved rows stay in the editor.
    const latest = await api(`bases/${kb}/entities`), items = [...latest.items];
    const index = row.original ? items.findIndex(item => item.name === row.original.name) : -1;
    if (row.original && (index < 0 || JSON.stringify(items[index]) !== JSON.stringify(row.original))) throw new Error('此实体已在其他页面修改，请重新选择知识库后再编辑');
    if (action === 'delete') items.splice(index, 1);
    else if (index >= 0) items[index] = value;
    else items.push(value);
    const result = await api(`bases/${kb}/entities`, 'PUT', {items, expected_items: latest.items});
    if (action === 'delete') aliasRows = aliasRows.filter(item => item !== row);
    else {
      row.original = result.items.find(item => item.name === value.name);
      row.name = row.original.name; row.aliases = row.original.aliases.join('，');
    }
    toast(action === 'delete' ? '实体已删除' : `「${value.name}」已保存，下次提问生效`);
  } catch (error) { toast(error.message); }
  finally { aliasSaving = false; renderAliases(); }
};

let traceOffset = 0, traceTotal = 0, traceRequest = 0;
const traceModes = {model:'模型回答',document:'原文回退',handoff:'转人工',error:'异常',running:'处理中 / 未完成'};
const traceOrigins = {qq_group:'QQ 群聊',qq_private:'QQ 私聊',preview:'后台预览',api:'API'};
const traceDelivery = {pending:'未收到发送回执',delivered:'已发送',failed:'发送失败',not_applicable:'无需发送 QQ'};
const traceTime = value => new Date(value * 1000).toLocaleString('zh-CN');
async function loadTraces(reset = false) {
  if (reset) traceOffset = 0;
  const request = ++traceRequest, params = new URLSearchParams({offset:String(traceOffset)});
  for (const [field,id] of Object.entries({kb_id:'trace-base',q:'trace-query',origin:'trace-origin',mode:'trace-mode',user_id:'trace-user',group_id:'trace-group'})) if ($(id).value.trim()) params.set(field,$(id).value.trim());
  for (const field of ['start','end']) if ($('trace-'+field).value) params.set(field,String(new Date($('trace-'+field).value).getTime()/1000));
  if (params.has('start') && params.has('end') && +params.get('start') > +params.get('end')) throw new Error('开始时间不能晚于结束时间');
  $('trace-count').textContent = '正在查询…'; $('trace-prev').disabled = $('trace-next').disabled = true;
  try {
    const data = await api('traces?' + params);
    if (request !== traceRequest) return;
    traceTotal = data.total;
    $('trace-list').innerHTML = data.items.length ? data.items.map(row => `<tr><td>${esc(traceTime(row.created))}<small>${esc(traceOrigins[row.origin] || row.origin)} · ${esc(row.kb_name)}</small></td><td>${esc(row.question.slice(0,100))}${row.question.length>100?'…':''}<small class="trace-terms">${esc((row.search_terms||[]).map(group=>Array.isArray(group)?'['+group.join(' + ')+']':group).join(' / '))} · ${row.retrieval_count} 次检索</small><small>${esc(row.user_id || '—')}</small></td><td>${esc(row.answer.slice(0,140) || '尚无回复')}${row.answer.length>140?'…':''}</td><td><span class="badge ${row.mode==='model'?'':'pending'}">${esc(traceModes[row.mode] || row.mode)}</span><small>${row.elapsed_ms} ms · ${esc(traceDelivery[row.delivery] || row.delivery)}</small></td><td><button class="row-button" data-trace="${row.id}">查看详情</button></td></tr>`).join('') : '<tr><td colspan="5" class="empty">没有符合条件的记录。新对话将自动记录，超过7天的记录会清理。</td></tr>';
    $('trace-count').textContent = `共 ${data.total} 条 · 第 ${Math.floor(data.offset/30)+1} 页 · 保留7天`;
    $('trace-prev').disabled = !traceOffset; $('trace-next').disabled = traceOffset + 30 >= traceTotal;
  } catch (error) { if (request === traceRequest) $('trace-count').textContent = error.message; throw error; }
}
$('trace-filter').onsubmit = event => { event.preventDefault(); busy(event.submitter, () => loadTraces(true)); };
$('trace-prev').onclick = () => { traceOffset = Math.max(0,traceOffset-30); loadTraces().catch(error=>toast(error.message)); };
$('trace-next').onclick = () => { traceOffset += 30; loadTraces().catch(error=>toast(error.message)); };
$('close-trace').onclick = () => $('trace-dialog').close();
const traceText = value => `<div class="trace-text">${esc(value || '无')}</div>`;
$('trace-list').onclick = event => {
  const button = event.target.closest('[data-trace]');
  if (!button) return;
  busy(button, async () => {
    const row = await api('traces/'+button.dataset.trace), d = row.details;
    $('trace-detail').innerHTML = `<div class="trace-meta"><span>${esc(traceTime(row.created))}</span><span>${esc(traceOrigins[row.origin])} · ${esc(row.kb_name)}</span><span>${esc(traceModes[row.mode])} · ${row.elapsed_ms} ms</span><span>${esc(traceDelivery[row.delivery])}</span></div><small>Trace ID：${esc(row.id)}<br>用户：${esc(row.user_id || '—')}<br>群：${esc(row.group_id || '—')}<br>会话：${esc(row.session_id || '—')}</small><h3>用户问题</h3>${traceText(row.question)}<h3>生成的回复</h3>${traceText(row.answer)}<p class="hint">原因：${esc(answerReasons[row.reason] || row.reason || '未完成')}</p>${d.delivery?`<h3>QQ 实际发送内容</h3>${traceText(d.delivery.content)}<p class="hint">${esc(d.delivery.error || '')}</p>`:''}<h3>最终检索词</h3>${traceText((d.search_terms||[]).map(group=>Array.isArray(group)?'['+group.join(' + ')+']':group).join(' / '))}<h3>别名说明</h3>${traceText(d.alias_context)}<h3>召回过程</h3>${(d.retrievals||[]).map((search,index)=>`<details open><summary>第 ${index+1} 次召回 · ${search.elapsed_ms} ms · ${(search.searches||[]).length} 组查询 · ${(search.results||[]).length} 个去重分段</summary>${(search.searches||[]).map(group=>`<p class="hint">${esc(JSON.stringify(group.query))} · ${group.elapsed_ms} ms · ${group.hits.length} 个命中</p>${traceText(group.hits.map(hit=>hit.title+' · chunk '+hit.chunk_id+' · score '+hit.score).join('\n'))}`).join('')}${(search.results||[]).map(result=>`<details><summary>${esc(result.title)} · 分段 ${result.ordinal+1} · ${esc(result.document_id)}</summary>${traceText(result.content)}</details>`).join('')}</details>`).join('') || '<p class="hint">本次未执行检索。</p>'}<h3>模型调用</h3>${(d.model_calls||[]).map(call=>`<details><summary>${call.stage==='keywords'?'生成检索词':'生成回答'} · ${call.elapsed_ms} ms${call.error?' · '+esc(call.error):''}</summary>${traceText(call.output)}${call.truncated?'<p class="hint">输出已截断</p>':''}</details>`).join('') || '<p class="hint">本次未调用模型。</p>'}<details><summary>携带的历史问答 · ${(d.history||[]).length/2} 轮</summary>${(d.history||[]).map(message=>`<h4>${message.role==='user'?'用户':'机器人'}</h4>${traceText(message.content)}`).join('')}</details><details><summary>本次模型与提示词</summary><p>${esc(d.model || '')}</p><h4>回答 System Prompt</h4>${traceText(d.system_prompt)}<h4>检索词提示词</h4>${traceText(d.keyword_prompt)}</details>${d.error_type?`<p class="error">异常类型：${esc(d.error_type)}</p>`:''}`;
    $('trace-dialog').showModal();
  });
};
