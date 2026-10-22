const $ = (s, root = document) => root.querySelector(s);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = n => new Intl.NumberFormat('zh-CN').format(n || 0);
const when = value => value ? new Date(value).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '暂无记录';
const labels = {overview:'总览',projects:'项目',memory:'记忆与时间线',runtime:'运行状态',documents:'文档中心',handoff:'任务交接'};
const types = {decision:'决策',fact:'事实',progress:'进展',bugfix:'修复',risk:'风险',hypothesis:'待验证',test_result:'验证',config:'配置',preference:'偏好'};
const states = {active:'进行中',paused:'已暂停',closed:'已关闭',unknown:'状态未声明',conflict:'状态有冲突'};
const paths = {
  overview:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
  projects:'M3 7v13h18V5h-9l-2-2H3v4 M3 8h18',
  memory:'M9 3H5v18h14V3h-4 M9 2h6v4H9z M8 11h8 M8 15h6',
  runtime:'M3 12h4l3-8 4 16 3-8h4',
  documents:'M5 3h10l4 4v14H5z M14 3v5h5 M8 12h8 M8 16h6',
  search:'M21 21l-6-6 M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0',
  refresh:'M20 7v5h-5 M4 17v-5h5 M5 7a8 8 0 0 1 13-2l2 3 M19 17A8 8 0 0 1 6 19l-2-3',
  arrow:'M7 17L17 7 M7 7h10v10',
  clock:'M12 8v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  sun:'M12 2v2 M12 20v2 M2 12h2 M20 12h2 M5 5l1.5 1.5 M17.5 17.5L19 19 M5 19l1.5-1.5 M17.5 6.5L19 5 M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
  moon:'M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10Z',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.documents}"/></svg>`;
document.querySelectorAll('[data-icon]').forEach(el => el.innerHTML = icon(el.dataset.icon));
function updateThemeButton() {
  const dark = document.documentElement.dataset.theme === 'dark';
  const button = $('#theme-toggle');
  button.innerHTML = icon(dark ? 'sun' : 'moon');
  button.title = button.ariaLabel = dark ? '切换到浅色模式' : '切换到深色模式';
}
updateThemeButton();
$('#theme-toggle').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('backstage.theme', theme);
  updateThemeButton();
});
let overview, renderEpoch = 0, eventEpoch = 0;
const content = $('#content');
function route() { const [name, query] = location.hash.replace(/^#\/?/, '').split('?'); return {name:labels[name] ? name : 'overview', params:new URLSearchParams(query)}; }
function href(name, params = {}) { return `#/${name}?${new URLSearchParams(Object.entries(params).filter(([,v])=>v !== '' && v != null))}`; }
async function api(path, params = {}) {
  const response = await fetch(`api/${path}?${new URLSearchParams(params)}`, {cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}
function empty(message) { return `<div class="empty">${icon('search')}<strong>${escape(message)}</strong><p>试试调整筛选条件，或刷新工作区数据。</p></div>`; }
function heading(kicker, title, description, action = '') { return `<div class="page-heading"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }
function tag(text, color = '') { return `<span class="tag ${color}">${escape(text)}</span>`; }
function taskTag(state) { return tag(states[state] || state, state === 'active' ? 'green' : state === 'conflict' ? 'amber' : ''); }
function eventRows(events, compact = false) {
  return events.map(e => `<button class="event-row ${compact ? 'compact' : ''}" data-event="${escape(e.event_id)}"><span class="event-marker ${e.event_type === 'risk' ? 'amber' : e.event_type === 'decision' ? 'purple' : ''}">${icon(e.event_type === 'decision' ? 'memory' : 'clock')}</span><span class="event-main"><span class="event-top">${tag(types[e.event_type] || e.event_type, e.event_type === 'risk' ? 'amber' : '')}${e.replaced ? tag('已被取代') : ''}<time>${when(e.created_at)}</time></span><strong>${escape(e.title)}</strong>${compact ? '' : `<span class="event-excerpt">${escape(e.excerpt)}</span>`}<span class="event-meta">${escape(e.projects.join(' / ') || '工作区')}<i>·</i>${escape(e.agent)}<i>·</i>${escape(e.machine_id)}</span></span><span class="row-arrow">↗</span></button>`).join('');
}
function projectCards(projects) {
  return projects.map((p, i) => `<a class="project-card" href="${href('projects',{name:p.name})}"><div class="project-card-head"><span class="project-avatar tone-${i % 4}">${escape(p.name.replace(/[^a-z0-9\u4e00-\u9fff]/ig,'').slice(0,2).toUpperCase())}</span>${icon('arrow')}</div><h3>${escape(p.title || p.name)}</h3><p>${escape(p.description || p.latest_title || '等待项目的第一条记忆')}</p><div class="project-card-foot"><span>${p.active_tasks ? `<span class="status-dot"></span>${p.active_tasks} 个进行中任务` : '暂无已声明的活跃任务'}</span><span>${p.kind === 'knowledge' ? `${p.repositories.length} 个来源仓库` : `${number(p.events)} 条记忆`}</span></div></a>`).join('');
}
function serviceCards(items) {
  const names = {'claude-feishu':'Feishu Bridge','watchdog-train':'训练监控','watchdog-b1k':'B1K 评测监控'};
  return items.map(s => `<div class="service-card"><div class="service-top"><span class="service-icon">${icon(s.name === 'claude-feishu' ? 'memory' : 'runtime')}</span>${tag(s.running ? (s.ready ? '就绪' : '进程运行中') : '未运行', s.running && s.ready ? 'green' : '')}</div><h3>${names[s.name]}</h3><p>${s.running ? `PID ${s.pid} · ${s.name === 'claude-feishu' ? (s.ready ? 'WebSocket 已连接' : '连接未就绪') : '本机监控进程'}` : s.configured ? '本机已有配置，当前进程未运行' : '本机未发现运行进程'}</p>${s.running ? `<p class="service-version">启动版本：${escape(s.code_at_start?.version || '未知（旧记录）')}<br><code>${escape(s.code_at_start?.commit?.slice(0,12) || '无启动提交记录')}</code><br>${s.code_changed === true ? '启动后源码已变化' : s.code_changed === false ? '与本机源码版本一致' : '版本差异待核对'}</p>` : ''}</div>`).join('');
}
async function showOverview(epoch) {
  const o = overview;
  const stats = [['projects','业务项目',o.counts.projects,'来自项目知识'],['memory','结构化记忆',o.counts.events,'保留原始依据'],['runtime','进行中任务',o.counts.active_tasks,'依据显式任务状态'],['documents','可读文档',o.counts.documents,'Markdown 与 SVG']];
  const maxActivity = Math.max(1,...o.activity.map(d=>d.count));
  content.innerHTML = heading('WORKSPACE ACTIVITY','最近发生了什么。','先看进展，再回到项目中的决策与依据。',`<span class="date-chip">${icon('clock')}${new Date().toLocaleDateString('zh-CN',{month:'long',day:'numeric',weekday:'long'})}</span>`) +
    `<div class="overview-columns focus-grid"><section class="panel"><div class="panel-heading"><h2>最近发生</h2><a href="#/memory">打开时间线 ↗</a></div><div>${eventRows(o.recent.slice(0,5),true) || empty('还没有记忆')}</div></section>
    <div class="overview-side"><section class="panel attention-panel"><div class="panel-heading"><h2>需要留意</h2>${tag(`${o.counts.attention} 条待核对`,'amber')}</div><p class="panel-hint">未解决的风险与待验证记录，请结合来源判断。</p>${eventRows(o.attention.slice(0,3),true) || '<p class="panel-hint">暂无未解决的风险记录。</p>'}</section><section class="panel"><div class="panel-heading"><h2>本机服务快照</h2><a href="#/runtime">详情 ↗</a></div><div id="home-services" class="small-service-list"><p class="muted">正在读取进程状态…</p></div></section></div></div>
    <div class="section-heading"><h2>${o.project_mode === 'knowledge' ? '业务项目' : '来源仓库'} <span>${o.projects.length}</span></h2><a href="#/projects">查看全部 →</a></div><div class="projects-grid">${projectCards(o.projects.slice(0,3))}</div>
    <div class="stats-grid compact-stats">${stats.map(([name,title,value,note])=>`<a class="stat-card" href="${href(name)}"><div class="stat-label">${icon(name)}${title}</div><strong>${number(value)}</strong><span>${note}</span></a>`).join('')}</div>
    <details class="activity-disclosure"><summary>记忆记录活动 · 最近 ${o.activity.length} 个记录日</summary><div class="activity-bars">${o.activity.map(d=>`<div class="activity-bar" title="${d.date} · ${d.count} 条"><progress max="${maxActivity}" value="${d.count}"></progress></div>`).join('')}</div><div class="activity-legend"><span>按事件日期 · UTC</span><span>记录量不等于工作进展</span></div></details>`;
  const services = await api('services');
  if (epoch !== renderEpoch) return;
  $('#home-services').innerHTML = services.items.map(s=>`<div><span><span class="status-dot ${s.running && s.ready ? '' : 'muted-dot'}"></span>${escape(s.name)}</span><span>${s.running ? (s.ready ? '就绪' : '未就绪') : '未运行'}</span></div>`).join('');
}
async function showProjects(params, epoch) {
  const name = params.get('name');
  if (!name) { content.innerHTML = heading('PROJECTS','业务项目','按已整理的项目知识进入，跨仓库查看相关任务、记忆与文档。') + `<div class="projects-grid all-projects">${projectCards(overview.projects)}</div>${overview.project_mode !== 'knowledge' ? '<p class="muted">此工作区尚无项目知识，目前展示来源仓库。</p>' : ''}`; return; }
  const project = overview.projects.find(p=>p.name === name);
  if (!project) { content.innerHTML = empty('项目不存在'); return; }
  const business = project.kind === 'knowledge';
  const filter = business ? {business:name} : {project:name};
  const tasks = overview.tasks.filter(t=>(business ? t.business_projects : t.projects).includes(name));
  const repos = business ? project.repositories : [name];
  content.innerHTML = heading('PROJECT SPACE',escape(project.title || name),escape(project.description || project.path || '该项目的共享记忆'),`<a class="button" href="#/projects">← 全部项目</a>`) + `<div class="project-summary"><span>${number(project.events)} 条关联记忆</span><span>${number(project.active_tasks)} 个关联进行中任务</span><span>最近记录 ${when(project.last_event_at)}</span></div>${business ? `<div class="project-entry"><a class="button" href="${href('documents',{business:name,path:project.home})}">${icon('documents')}阅读项目知识</a><a class="button" href="${href('documents',{business:name})}">报告与架构图 · ${project.documents.length} 份</a><p>关联来自项目知识；原始仓库标签与来源保持不变。</p></div>` : ''}<div class="repository-links"><span>来源仓库</span>${repos.map(repo=>`<a class="tag" href="${href('memory',{...filter,project:repo})}">${escape(repo)}</a>`).join('')}</div><div class="overview-columns"><section class="panel"><div class="panel-heading"><h2>项目时间线</h2><a href="${href('memory',filter)}">全部记忆 ↗</a></div><div id="project-events" class="loading">正在读取记忆…</div></section><section class="panel"><div class="panel-heading"><h2>关联任务</h2><span>${tasks.length}</span></div><div class="task-list">${tasks.map(t=>`<a href="${href('handoff',{task:t.task_id})}">${taskTag(t.state)}<strong>${escape(t.task_id)}</strong></a>`).join('') || empty('尚无关联任务')}</div></section></div>`;
  const data = await api('events',{...filter,limit:15});
  if (epoch !== renderEpoch) return;
  $('#project-events').className = '';
  $('#project-events').innerHTML = eventRows(data.items) || empty('暂无项目记忆');
}
function select(name, label, options, value, allLabel = '全部') { return `<label class="filter-select"><span>${label}</span><select name="${name}" aria-label="${label}"><option value="">${escape(allLabel)}</option>${options.map(([v,t])=>`<option value="${escape(v)}" ${v === value ? 'selected' : ''}>${escape(t)}</option>`).join('')}</select></label>`; }
async function showMemory(params, epoch) {
  content.innerHTML = heading('MEMORY & TIMELINE','记忆，串成时间线。','按项目、机器或 Agent 找到进展，展开查看原始依据。') +
    `<form id="memory-filters" class="filters"><div class="memory-search">${icon('search')}<input name="q" aria-label="搜索记忆" placeholder="搜索记忆内容、决策与进展…" value="${escape(params.get('q') || '')}"><button class="button" type="submit">搜索</button></div><div class="filter-row">${overview.project_mode === 'knowledge' ? select('business','业务项目',overview.projects.map(p=>[p.name,p.title]),params.get('business')) : ''}${select('project','来源仓库',overview.repositories.map(p=>[p.name,p.name]),params.get('project'))}${select('agent','Agent',overview.agents.map(a=>[a,a]),params.get('agent'))}${select('machine','机器',overview.machines.map(m=>[m.name,m.name]),params.get('machine'))}${select('type','记录类型',Object.entries(types),params.get('type'))}${select('sort','排序',[['relevance','相关性'],['recent','时间倒序']],params.get('sort'),'自动')}<label class="history-filter"><input name="history" type="checkbox" ${params.get('current') === 'false' ? 'checked' : ''}>包含已被取代的记录</label></div>${params.get('task') ? `<div class="task-filter">任务：${escape(params.get('task'))}<button type="button" id="clear-task" class="text-button">清除 ×</button><input type="hidden" name="task" value="${escape(params.get('task'))}"></div>` : ''}</form><div id="memory-results" class="loading">正在读取时间线…</div>`;
  $('#memory-filters').addEventListener('submit', e=>{ e.preventDefault(); const data = Object.fromEntries(new FormData(e.currentTarget)); delete data.history; data.current = e.currentTarget.history.checked ? 'false' : 'true'; location.hash = href('memory',data); });
  $('#memory-filters').addEventListener('change',e=>{ if(e.target.tagName === 'SELECT' || e.target.type === 'checkbox') $('#memory-filters').requestSubmit(); });
  $('#clear-task')?.addEventListener('click',()=>{params.delete('task'); location.hash=href('memory',Object.fromEntries(params));});
  const data = await api('events',Object.fromEntries(params));
  if (epoch !== renderEpoch) return;
  const offset = Number(params.get('offset') || 0), size=30;
  $('#memory-results').className = '';
  $('#memory-results').innerHTML = `<div class="result-count">${number(data.total)} 条匹配记录 ${params.get('current') === 'false' ? '· 包含历史' : '· 已排除显式被取代的记录'}</div>${params.get('q') ? `<a class="button search-documents" href="${href('documents',{all:'true',business:params.get('business'),q:params.get('q')})}">搜索文档正文 ↗</a>` : ''}<section class="panel timeline">${eventRows(data.items) || empty('没有找到匹配记忆')}</section><div class="pagination"><span>${data.total ? offset+1 : 0}–${Math.min(offset+size,data.total)} / ${number(data.total)}</span><div>${offset ? `<a class="button" href="${href('memory',{...Object.fromEntries(params),offset:Math.max(0,offset-size)})}">上一页</a>` : ''}${offset+size < data.total ? `<a class="button" href="${href('memory',{...Object.fromEntries(params),offset:offset+size})}">下一页 →</a>` : ''}</div></div>`;
}
async function showDocuments(params, epoch) {
  const business=params.get('business');
  const project=overview.projects.find(p=>p.name===business && p.kind==='knowledge');
  if (!business && !params.get('path') && !params.get('q') && !params.has('all') && overview.project_mode==='knowledge') {
    content.innerHTML=heading('KNOWLEDGE LIBRARY','从项目开始阅读。','先选择业务项目，再打开它的知识、实验报告与架构图。',`<a class="button" href="${href('documents',{all:'true'})}">全部文档 →</a>`) + `<div class="projects-grid document-projects">${overview.projects.map((p,i)=>`<a class="project-card" href="${href('documents',{business:p.name})}"><div class="project-card-head"><span class="project-avatar tone-${i%4}">${icon('documents')}</span>${icon('arrow')}</div><h3>${escape(p.title)}</h3><p>${escape(p.description)}</p><div class="project-card-foot"><span>项目知识 · 报告 · 架构图</span><span>${p.documents.length} 份文档</span></div></a>`).join('')}</div>`;
    return;
  }
  const data = await api('documents',{...(business ? {business} : {}),q:params.get('q') || ''});
  if (epoch !== renderEpoch) return;
  const path = params.get('path') || project?.home, query=params.get('q') || '';
  const docs = data.items;
  const context=business ? {business} : {all:'true'};
  content.innerHTML = heading('KNOWLEDGE LIBRARY',escape(project?.title || '全部文档'),project ? '围绕项目阅读；文档保留原有路径和来源。' : '通用说明和未归属文档也可在这里查找。',`<a class="button" href="#/documents">← 按项目阅读</a>`) + `<div class="document-layout"><aside class="document-list"><form id="document-filter" class="document-search">${icon('search')}<input aria-label="筛选文档" name="q" placeholder="搜索标题、路径或正文…" value="${escape(query)}"></form><div id="document-links">${docs.map(d=>`<a href="${href('documents',{...context,path:d.path,q:query})}" class="document-link ${path === d.path ? 'selected' : ''}">${icon('documents')}<span><strong>${escape(d.title)}</strong><small>${escape(d.path)}</small>${d.excerpt ? `<small class="document-excerpt">${escape(d.excerpt)}</small>` : ''}</span><span class="file-kind">${d.kind.toUpperCase()}</span></a>`).join('') || empty('没有匹配的文档')}</div></aside><section class="document-preview panel" id="document-preview"><div class="document-placeholder">${icon('documents')}<h2>让知识触手可及。</h2><p>选择一份文档，开始阅读。</p><span>${number(data.items.length)} 份 Markdown / SVG 文档</span></div></section></div>`;
  $('#document-filter').addEventListener('submit',e=>{e.preventDefault();location.hash=href('documents',{...context,q:new FormData(e.currentTarget).get('q'),path});});
  if (!path) return;
  const doc = await api('document',{path});
  if (epoch !== renderEpoch) return;
  $('#document-preview').innerHTML = `<div class="document-toolbar"><span>${escape(path)}</span>${doc.updated_at ? `<time>文件更新 ${when(doc.updated_at)}</time>` : ''}</div>${doc.kind === 'svg' ? `<iframe class="svg-preview" title="SVG 文档预览" sandbox src="${escape(doc.asset)}"></iframe>` : `<article class="prose">${doc.html}</article>`}`;
  if (doc.kind === 'md') {
    document.querySelectorAll('.prose table').forEach(table=>{const scroll=document.createElement('div');scroll.className='table-scroll';scroll.tabIndex=0;scroll.setAttribute('role','region');scroll.setAttribute('aria-label','文档表格，可横向滚动');table.before(scroll);scroll.append(table);});
    document.querySelectorAll('.prose h1,.prose h2,.prose h3,.prose h4').forEach(h=>h.id='doc-'+h.textContent.trim().toLowerCase().replace(/[^\p{L}\p{N}_\s-]/gu,'').replace(/\s+/g,'-'));
    if (business) document.querySelectorAll('.prose a[href^="#/documents?"]').forEach(a=>{const next=new URLSearchParams(a.hash.split('?')[1]);next.set('business',business);a.href=href('documents',Object.fromEntries(next));});
    if (params.get('anchor')) document.getElementById('doc-'+params.get('anchor'))?.scrollIntoView({block:'start'});
  }
}
function doctorDetails(data) {
  const code=data.source_code, health=data.memory_health;
  return `<div class="doctor-evidence"><h3>本机源码与 Skill</h3><p>工作区核心：${escape(code.version || '未知')} · ${code.dirty === true ? '有未提交改动' : code.dirty === false ? '源码干净' : '改动状态未知'}<br><code>${escape(code.commit || '未读取到 Git 提交')}</code></p><p class="panel-hint">${data.skills.map(s=>`${escape(s.name)}：最低版本 ${escape(s.minimum_version || '未声明')}（${escape({compatible:'满足版本声明',outdated:'核心版本过旧',unknown:'兼容性未知',invalid:'声明无效',unreadable:'不可读'}[s.state] || s.state)}）`).join('<br>') || '暂无可检查的 Skill'}</p></div><details class="health-details"><summary>记忆健康明细 · ${Object.values(health.counts).reduce((a,b)=>a+b,0)} 项候选</summary><p class="panel-hint">检查 ${health.checked_events} 条记忆；超过 ${health.stale_days} 天的进展、风险与假设可能需要重新核对。候选问题不代表记录失效。</p><div class="health-list">${health.items.map(item=>`<${item.event_id ? `button data-event="${escape(item.event_id)}"` : 'div'} class="health-record"><strong>${escape(item.label)}</strong><span>${escape(item.title || item.detail || item.task_id || '核对 memory validate / audit')}</span>${item.source ? `<code>${escape(item.source)}</code>` : ''}</${item.event_id ? 'button' : 'div'}>`).join('') || '<p class="panel-hint">未发现审计候选问题。</p>'}</div>${health.omitted ? `<p class="panel-hint">另 ${health.omitted} 项请用 memory audit 查看。</p>` : ''}</details>`;
}
async function showRuntime(params, epoch) {
  const services = await api('services');
  if (epoch !== renderEpoch) return;
  content.innerHTML = heading('LOCAL RUNTIME SNAPSHOT','本机运行快照。','读取本机服务与已配置监控。更新时间表示本次读取，不代表远端机器在线。',`<span class="date-chip">${icon('clock')}读取于 ${when(services.read_at)}</span>`) + `<div class="runtime-scope"><span>范围：本机</span><span>工具核心 ${escape(services.installed_code.version || '版本未知')} · ${escape(services.installed_code.commit?.slice(0,12) || '提交未知')}</span><span>来源：本机进程核验与服务 PID 记录</span><span>采样时间：${when(services.sampled_at)}</span></div><div class="services-grid">${serviceCards(services.items)}</div><div class="section-heading"><h2>训练与评测</h2><span class="muted">只读快照 · 手动刷新</span></div><div class="monitor-grid">${[['train','训练 / 离线评测','TRAINING'],['b1k','B1K 评测','EVALUATION']].map(([kind,title,label])=>`<section class="panel monitor-panel"><div class="panel-heading"><h2>${title}</h2><span>${label}</span></div><div id="monitor-${kind}" class="loading">正在读取已配置监控…</div></section>`).join('')}</div><div class="section-heading"><h2>记忆来源机器</h2><span class="muted">这是事件最后记录时间，不是在线探测</span></div><div class="machines-grid">${overview.machines.map(m=>`<a class="machine-card" href="${href('memory',{machine:m.name})}"><span class="machine-symbol">${icon('overview')}</span><strong>${escape(m.name)}</strong><span>最后事件 ${when(m.last_event_at)}</span>${icon('arrow')}</a>`).join('')}</div>`;
  content.insertAdjacentHTML('beforeend','<section class="panel doctor-panel"><div class="panel-heading"><h2>工作区自检</h2><span>本机只读 · 不连接远端</span></div><div id="doctor-results" class="loading">正在检查配置、记忆与链接…</div></section>');
  const doctorRequest = api('doctor').then(data=>{
    if(epoch !== renderEpoch) return;
    $('#doctor-results').className='doctor-checks';
    $('#doctor-results').innerHTML=data.checks.map(c=>`<div class="doctor-check">${tag({ok:'通过',warning:'待核对',error:'有错误',unconfigured:'未配置'}[c.state],c.state==='ok'?'green':c.state==='error'||c.state==='warning'?'amber':'')}<div><strong>${escape(c.name)}</strong><p>${escape(c.detail)}</p>${c.hint ? `<small>${escape(c.hint)}</small>`:''}</div></div>`).join('')+doctorDetails(data)+`<p class="panel-hint">${escape(data.notice)} · ${when(data.sampled_at)}<br>本机状态来源：${escape(data.local_root)}<br>各机器最后记忆时间见上方列表。</p>`;
  }).catch(error=>{if(epoch === renderEpoch) $('#doctor-results').innerHTML=`<p class="inline-error">${escape(error.message)}</p>`;});
  await Promise.all(['train','b1k'].map(async kind=>{
    try {
      const data=await api('monitor',{kind});
      if (epoch !== renderEpoch) return;
      $(`#monitor-${kind}`).className='monitor-content';
      $(`#monitor-${kind}`).innerHTML=data.error ? `<div class="inline-error">${escape(data.error)}</div>` : !data.configured ? `<div class="monitor-empty"><span class="muted-dot status-dot"></span><h3>尚未配置</h3><p>添加现有 watchdog 配置后，可在这里读取状态。</p></div>` : `<pre class="monitor-status">${escape(data.status || '本次采集未返回活跃任务详情。')}</pre>`;
      $(`#monitor-${kind}`).insertAdjacentHTML('beforeend',`<div class="sample-meta"><span>范围：本机 · ${escape({available:'有采集结果',unconfigured:'未配置',unavailable:'不可用',no_data:'无数据'}[data.state] || '状态未知')}</span><span>采样时间：${when(data.sampled_at)}</span><span>来源：${escape(data.collector)}</span><code>${escape(data.source)}</code></div>`);
    } catch(error) { if(epoch === renderEpoch) { $(`#monitor-${kind}`).className='monitor-content'; $(`#monitor-${kind}`).innerHTML=`<div class="inline-error">${escape(error.message)}</div>`; } }
  }));
  await doctorRequest;
}
async function showHandoff(params, epoch) {
  const task=params.get('task');
  content.innerHTML=heading('TASK HANDOFF','接续一项工作。','按任务查看进展、失败尝试、验证和未决问题，保留每条记录的来源。')+`<form id="handoff-filter" class="filters"><label class="filter-select"><span>选择任务</span><select name="task" aria-label="选择交接任务"><option value="">选择一项任务</option>${overview.tasks.map(t=>`<option value="${escape(t.task_id)}" ${t.task_id===task?'selected':''}>${escape(t.task_id)} · ${escape(states[t.state])}</option>`).join('')}</select></label></form><div id="handoff-content">${empty('选择任务，查看交接内容')}</div>`;
  $('#handoff-filter').addEventListener('change',e=>{location.hash=href('handoff',{task:e.target.value});});
  if(!task) return;
  const data=await api('handoff',{task});
  if(epoch !== renderEpoch) return;
  $('#handoff-content').innerHTML=`<div class="project-summary"><strong>${escape(data.task_id)}</strong>${taskTag(data.state)}<a class="button" href="${href('memory',{task})}">查看全部记忆 ↗</a></div><p class="panel-hint">${escape(data.notice)}</p>${data.errors.length ? '<p class="data-warning">来源数据不完整，请先核对工作区自检。</p>':''}${data.heads.length ? `<section class="panel handoff-state"><div class="panel-heading"><h2>任务状态依据</h2></div>${data.heads.map(h=>`<button class="reference-link" data-event="${escape(h.event_id)}">${escape(states[h.content.task_state])} · ${escape(h.content.what)} ↗</button>`).join('')}</section>`:''}<div class="handoff-grid">${Object.entries(data.sections).map(([name,rows])=>`<section class="panel"><div class="panel-heading"><h2>${escape(name)}</h2>${data.omitted[name] ? `<span>另 ${data.omitted[name]} 条见全部记忆</span>`:''}</div>${rows.length ? rows.map(e=>`<button class="handoff-record" data-event="${escape(e.event_id)}"><strong>${escape(e.title)}</strong><p>${escape(e.text)}</p><small>${when(e.created_at)} · ${escape(e.type)} · ${escape(e.status)}</small><code>${escape(e.event_id)}</code></button>`).join(''):'<p class="panel-hint">暂无对应记录</p>'}</section>`).join('')}</div><section class="panel handoff-repositories"><div class="panel-heading"><h2>当前本机仓库</h2><span>读取时状态</span></div>${data.repositories.map(r=>`<div class="handoff-repository"><strong>${escape(r.name)}</strong><span>${escape(r.branch || '未读取到分支')} · ${!r.commit?'仓库状态不可用':r.dirty===null?'工作区改动状态不可用':r.dirty?'有未提交改动':'工作区干净'}</span><code>${escape(r.commit || '未读取到提交')}</code></div>`).join('') || '<p class="panel-hint">暂无已注册来源仓库</p>'}${data.unregistered_repositories.length ? `<p class="panel-hint">本机未注册：${escape(data.unregistered_repositories.join('、'))}</p>`:''}</section>`;
}
async function openEvent(id) {
  const dialog=$('#event-dialog'), epoch=++eventEpoch;
  $('#event-detail').innerHTML='<div class="loading">正在读取原始依据…</div>';
  dialog.scrollTop=0;
  if(!dialog.open) dialog.showModal();
  try {
    const data=await api('event',{id}), e=data.event;
    if(epoch !== eventEpoch || !dialog.open) return;
    const fields=[['记录内容','what'],['原因与背景','why'],['来源位置','where'],['验证依据','verified_by'],['后续候选','next']];
    $('#event-detail').innerHTML=`<div class="eyebrow">MEMORY DETAIL</div><div class="detail-tags">${tag(types[e.event_type] || e.event_type)}${tag(e.status)}${data.replaced ? tag('已被取代','amber') : ''}</div><h2 id="drawer-title">${escape(e.title)}</h2><div class="detail-meta">${escape(e.agent)} · ${escape(e.machine_id)}<br>${new Date(e.created_at).toLocaleString('zh-CN')}</div>${e.task_id ? `<a class="task-filter detail-task" href="${href('memory',{task:e.task_id})}">任务 ${escape(e.task_id)} ↗</a><a class="button" href="${href('handoff',{task:e.task_id})}">查看任务交接 ↗</a>` : ''}${fields.filter(([,key])=>e.content[key]?.length).map(([title,key])=>`<section class="detail-section"><h3>${title}</h3><p>${escape(Array.isArray(e.content[key]) ? e.content[key].join('\n') : e.content[key])}</p></section>`).join('')}<section class="detail-section"><h3>来源文件</h3><code>${escape(data.source)}</code></section>${e.supersedes?.length ? `<section class="detail-section"><h3>修正了这些记录</h3>${e.supersedes.map(key=>`<button class="reference-link" data-event="${escape(key)}">${escape(key)} ↗</button>`).join('')}</section>` : ''}${data.replaced_by.length ? `<section class="detail-section"><h3>后续修正</h3>${data.replaced_by.map(key=>`<button class="reference-link" data-event="${escape(key)}">${escape(key)} ↗</button>`).join('')}</section>` : ''}<section class="detail-section" id="event-context"><h3>事件上下文</h3><p>正在读取邻域与修正链…</p></section><details class="raw-event"><summary>查看结构化原始事件</summary><pre>${escape(JSON.stringify(e,null,2))}</pre></details>`;
    try {
      const context=await api('context',{id});
      if(epoch !== eventEpoch || !dialog.open) return;
      $('#event-context').innerHTML=`<h3>事件上下文</h3><p class="panel-hint">${context.scope.task_id ? `同任务 · ${escape(context.scope.task_id)} · ${escape(states[context.task_state] || context.task_state)}` : '同 topic 与项目集合'} · ${context.scope_total} 条记录</p>${context.errors.length ? '<p class="data-warning">部分来源数据无效，当前上下文可能不完整。</p>' : ''}${context.warnings.map(w=>`<p class="data-warning">${escape(w)}</p>`).join('')}${context.items.map(item=>`<button class="context-record" data-event="${escape(item.event_id)}" ${item.anchor ? 'disabled aria-current="true"' : ''}><span>${item.anchor ? '当前记录' : item.correction ? '修正关系' : '相邻记录'} · ${when(item.created_at)}${item.replaced ? ' · 已被取代' : ''}</span><strong>${escape(item.title)}</strong><p>${escape(item.excerpt)}</p><code>${escape(item.event_id)}</code></button>`).join('')}<p class="panel-hint">${escape(context.notice)}<br>邻域另有 ${context.omitted_neighbors} 条；${context.corrections_truncated ? '修正链有省略，可沿记录继续查看。' : '修正链已列出。'}</p>`;
    } catch(error) { if(epoch === eventEpoch && dialog.open) $('#event-context').innerHTML=`<h3>事件上下文</h3><p class="inline-error">${escape(error.message)}</p>`; }
  } catch(error) { if(epoch !== eventEpoch || !dialog.open) return; $('#event-detail').innerHTML=`<div class="inline-error">${escape(error.message)}</div>`; }
}
async function render() {
  const epoch=++renderEpoch, {name,params}=route();
  ++eventEpoch;
  window.scrollTo(0,0);
  document.body.dataset.view=name;
  $('#event-dialog').close();
  $('#breadcrumb').textContent=labels[name];
  document.title=`${labels[name]} · Backstage`;
  document.querySelectorAll('[data-nav]').forEach(a=>{a.classList.toggle('active',a.dataset.nav===name);if(a.dataset.nav===name)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  content.innerHTML='<div class="loading">正在读取工作区…</div>';
  try {
    overview ||= await api('overview');
    if(epoch !== renderEpoch) return;
    $('#workspace-name').textContent=overview.workspace;
    $('#machine-name').textContent=overview.machine_id;
    $('#project-count').textContent=overview.counts.projects;
    $('#version').textContent=overview.version ? `v${overview.version}` : 'Backstage';
    $('#last-read').textContent=`总览采样 ${when(overview.sampled_at)} · 快照最多复用 ${overview.cache_seconds} 秒`;
    await ({overview:()=>showOverview(epoch),projects:()=>showProjects(params,epoch),memory:()=>showMemory(params,epoch),documents:()=>showDocuments(params,epoch),runtime:()=>showRuntime(params,epoch),handoff:()=>showHandoff(params,epoch)})[name]();
    if(epoch === renderEpoch && overview.errors.length) content.insertAdjacentHTML('afterbegin',`<div class="data-warning">工作区有 ${overview.errors.length} 项数据校验问题；当前展示有效记录，任务统计可能不完整。请运行 memory validate / audit 核对。</div>`);
  } catch(error) { if(epoch === renderEpoch) content.innerHTML=`<div class="error-page"><h1>暂时无法读取</h1><p>${escape(error.message)}</p><p>检查工作区配置后，点击右上角刷新。</p></div>`; }
}
document.addEventListener('click',e=>{const row=e.target.closest('[data-event]');if(row)openEvent(row.dataset.event);});
$('#close-drawer').addEventListener('click',()=>$('#event-dialog').close());
$('#event-dialog').addEventListener('click',e=>{if(e.target===$('#event-dialog'))$('#event-dialog').close();});
$('#global-search').addEventListener('submit',e=>{e.preventDefault();location.hash=href('memory',{q:$('input',e.currentTarget).value});});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#global-search input').focus();}});
$('#refresh').addEventListener('click',()=>{overview=null;render();});
window.addEventListener('hashchange',render);
render();
