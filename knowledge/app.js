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
const views = { documents: ['知识库', '把分散的信息，变成有据可依的回答。'], retrieve: ['召回测试', '在连接大模型之前，先看看知识是否被准确找到。'], integration: ['API 接入', '把你的知识，接入任意大模型工作流。'], settings: ['模型设置', '为你的知识库，连接语义理解能力。'] };
document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => {
  const view = button.dataset.view;
  document.querySelectorAll('.view').forEach(el => el.hidden = el.id !== 'view-' + view);
  document.querySelectorAll('[data-view]').forEach(el => el.classList.toggle('active', el === button));
  $('page-title').textContent = $('breadcrumb').textContent = views[view][0]; $('page-subtitle').textContent = views[view][1];
  $('create-base').hidden = view !== 'documents';
});
async function refresh() {
  const { items } = await api('bases'); state.bases = items;
  if (!items.some(b => b.id === state.selected)) state.selected = items[0]?.id || '';
  $('base-count').textContent = items.length;
  $('stats').innerHTML = [ ['知识库', items.length, '▤'], ['文档总数', items.reduce((s, b) => s + b.document_count, 0), '▧'], ['可检索分段', items.reduce((s, b) => s + b.chunk_count, 0), '⌘'] ].map(([title, count, icon]) => `<div class="stat"><div><small>${title}</small><strong>${count.toLocaleString()}</strong></div><div class="stat-icon">${icon}</div></div>`).join('');
  $('base-list').innerHTML = items.map(b => `<button class="base-card ${b.id === state.selected ? 'selected' : ''}" data-base="${b.id}"><div class="base-card-top"><span class="base-icon">▤</span><span class="badge ${b.vector_count < b.chunk_count ? 'pending' : ''}">${b.chunk_count && b.vector_count === b.chunk_count ? '向量已就绪' : '关键词检索'}</span></div><h3>${esc(b.name)}</h3><p>${esc(b.description || '为大模型准备有来源的知识')}</p><div class="base-card-bottom"><span>${b.document_count} 份文档</span><span>${b.chunk_count} 个分段</span><span>${b.vector_count} 个向量</span></div></button>`).join('');
  $('no-base').hidden = items.length > 0; $('document-panel').hidden = !state.selected;
  for (const id of ['retrieve-base', 'api-base', 'answer-base']) {
    const previous = $(id).value;
    $(id).innerHTML = items.map(b => `<option value="${b.id}">${esc(b.name)}</option>`).join('');
    $(id).value = items.some(b => b.id === previous) ? previous : state.selected;
  }
  updateExample();
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
    const result = await api('retrieve', 'POST', { kb_id: $('retrieve-base').value, query: $('retrieve-query').value, mode: $('retrieve-mode').value, top_k: +$('retrieve-top-k').value, max_context_chars: +$('context-budget').value });
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

const answerReasons = {ok:'模型工作正常',missing_key:'尚未填写 API Key，将返回最相关文档',disabled:'模型已关闭，将返回最相关文档',invalid_key:'API Key 无效，将返回最相关文档',insufficient_balance:'余额不足，将返回最相关文档',access_denied:'模型无访问权限，将返回最相关文档',rate_limited:'模型限流，已回退原文',network_error:'模型网络异常或超时，已回退原文',upstream_error:'模型服务异常，已回退原文',invalid_response:'模型响应异常，已回退原文',invalid_evidence:'模型引用证据未通过校验，已回退原文',no_results:'没有检索命中，已转人工',insufficient_evidence:'资料不足以回答，已转人工',busy:'模型请求较多，已回退原文'};
let answerDefaults = '';
async function loadAnswerSettings() {
  const cfg = await api('answer-settings');
  answerDefaults = cfg.default_prompt;
  $('answer-enabled').checked = cfg.enabled;
  $('answer-model').value = cfg.model;
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
  await api('answer-settings', 'PUT', { enabled: $('answer-enabled').checked, model: $('answer-model').value, api_key: $('answer-key').value, clear_key: $('answer-clear-key').checked, system_prompt: $('answer-prompt').value, handoff_groups: groups });
  await loadAnswerSettings(); $('answer-test-status').textContent = ''; toast('客服配置已保存，下次提问立即生效');
}); };
$('test-answer-model').onclick = () => busy($('test-answer-model'), async () => {
  try { const result = await api('answer-settings/test', 'POST', {}); $('answer-test-status').textContent = `连接成功 · ${result.model}`; }
  catch (error) { $('answer-test-status').textContent = error.message.replace(/invalid_key|insufficient_balance|access_denied|rate_limited|network_error|upstream_error|invalid_response|invalid_evidence/g, key => answerReasons[key]); }
  const cfg = await api('answer-settings');
  if (cfg.last_status) $('answer-status').textContent = answerReasons[cfg.last_status.reason] || cfg.last_status.reason;
});
$('answer-preview-form').onsubmit = event => { event.preventDefault(); busy(event.submitter, async () => {
  $('answer-preview').textContent = '正在检索并生成回复…';
  try {
    const result = await api('answer', 'POST', {kb_id: $('answer-base').value, query: $('answer-query').value});
    $('answer-preview').textContent = (answerReasons[result.reason] || result.mode) + '\n\n' + result.answer + (result.handoff ? '\n\n实际群聊会按该群的人工联系人配置尝试艾特；这里仅预览文本。' : '');
  } catch (error) { $('answer-preview').textContent = error.message; throw error; }
}); };
