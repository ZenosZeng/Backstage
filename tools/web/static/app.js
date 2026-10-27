import {mountCluster} from './cluster.js';

const $ = (s, root = document) => root.querySelector(s);
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = n => new Intl.NumberFormat('zh-CN').format(n || 0);
const when = value => value ? new Date(value).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '暂无记录';
const labels = {projects:'项目时间线',documents:'项目文档',runtime:'监控'};
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
let overview, renderEpoch = 0, eventEpoch = 0, toolEpoch = 0, runtimeCleanup = ()=>{};
const content = $('#content');
function route() { const [name, query] = location.hash.replace(/^#\/?/, '').split('?'); const params=new URLSearchParams(query); if(name==='memory' && params.get('business')) params.set('name',params.get('business')); return {name:labels[name] ? name : 'projects', params}; }
function href(name, params = {}) { return `#/${name}?${new URLSearchParams(Object.entries(params).filter(([,v])=>v !== '' && v != null))}`; }
async function api(path, params = {}) {
  const response = await fetch(`api/${path}?${new URLSearchParams(params)}`, {cache:'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}
function empty(message) { return `<div class="empty">${icon('search')}<strong>${escape(message)}</strong><p>试试调整筛选条件，或刷新工作区数据。</p></div>`; }
function heading(kicker, title, description, action = '') { return `<div class="page-heading journey-heading"><div><div class="eyebrow">${kicker}</div><h1>${title}</h1><p>${description}</p></div>${action}</div>`; }
function tag(text, color = '') { return `<span class="tag ${color}">${escape(text)}</span>`; }
function taskTag(state) { return tag(states[state] || state, state === 'active' ? 'green' : state === 'conflict' ? 'amber' : ''); }
function executionTag(execution) {
  const state=execution?.state || 'unknown';
  return tag({running:'Agent 运行中',waiting:'Agent 等待输入',error:'Agent 报告错误',idle:'Agent 空闲',unknown:'执行状态未知'}[state] || '执行状态未知', ['waiting','error'].includes(state)?'amber':state==='running'?'green':'');
}
function freshnessTag(value) {
  const state=value?.state || 'unknown';
  return tag({current:'覆盖源码未变',stale:'覆盖源码已变',needs_review:'有新记录待核验',unknown:'核验信息不足'}[state] || '核验信息不足', state==='current'?'green':['stale','needs_review'].includes(state)?'amber':'');
}
function freshnessPanel(value) {
  if(!value) return '';
  return `<details class="knowledge-freshness"><summary>${freshnessTag(value)}<span>知识来源与核验</span></summary><p>最后核验：${value.verified_at?when(value.verified_at):'未记录'} · 本次检查：${when(value.checked_at)}</p><p>源码未变化只说明覆盖范围与基线一致，内容仍需结合实际验证。</p>${(value.sources || []).map(source=>`<div><strong>${escape(source.repository)}</strong><code>${escape(source.commit)} → ${escape(source.current_commit || '当前提交不可用')}</code><p>覆盖：${escape((source.covers || []).join('、'))}</p>${source.changed_paths?.length?`<p>变化：${escape(source.changed_paths.join('、'))}</p>`:''}${(source.diagnostics || []).map(d=>`<p>${escape(d)}</p>`).join('')}</div>`).join('')}${(value.diagnostics || []).map(d=>`<p>${escape(d)}</p>`).join('')}${(value.source_event_ids || []).slice(0,5).map(id=>`<button class="reference-link" data-event="${escape(id)}">查看核验来源 ${escape(id)} ↗</button>`).join('')}</details>`;
}
function attentionPanel(data, projectName) {
  if(!data.attention_count) return '';
  return `<section class="panel task-attention"><div class="panel-heading"><h2>需要关注</h2><span>${number(data.attention_count)} 项任务 · 按已记录依据</span></div>${data.attention.map(item=>`<a class="attention-item" href="${href('projects',{name:projectName || item.projects[0],task:item.task_id})}"><strong>${escape(item.task_id)}</strong><span>${escape(item.reasons.join(' · '))}</span>${taskTag(item.state)}</a>`).join('')}${data.attention_count>data.attention.length?'<p class="panel-hint">仅展示前 8 项，进入项目继续查看。</p>':''}</section>`;
}
function taskContext(detail) {
  if(!detail) return '';
  const execution=detail.execution;
  const records=(title,items)=>`<section><h3>${title}</h3>${items.length?items.map(item=>`<button class="context-record" data-event="${escape(item.event_id)}"><strong>${escape(item.title)}</strong><span>${escape(item.excerpt)}</span><small>${when(item.created_at)} · 查看依据 ↗</small></button>`).join(''):'<p class="panel-hint">尚无相关结论。</p>'}</section>`;
  return `<section class="panel task-context"><div class="panel-heading"><h2>任务脉络 · 跨项目</h2>${executionTag(execution)}</div><div class="execution-observation"><p>${escape(execution.notice || '')}</p><span>执行观测：${when(execution.observed_at)} · 有效期 ${execution.fresh_seconds / 60} 分钟</span>${execution.event_id?`<button class="text-button" data-event="${escape(execution.event_id)}">查看执行观测依据 ↗</button>`:''}${execution.sessions.length?`<details class="session-execution"><summary>各会话执行观测 · ${execution.sessions.length} 个</summary>${execution.sessions.map(item=>`<div>${executionTag(item)}<span>${escape(item.session.agent)} · ${escape(item.session.machine_id)} · ${escape(item.session.session_id)}</span><span>${when(item.observed_at)} · ${escape(item.reason || '观测有效')}</span><button class="reference-link" data-event="${escape(item.event_id)}">查看观测依据 ↗</button></div>`).join('')}</details>`:''}</div><div class="task-conclusions">${records('最近决策',detail.decisions)}${records('验证与成果',detail.results)}${records('待核对事项',detail.attention)}</div><details class="session-references"><summary>关联 Agent 会话 · ${detail.sessions.length} 个</summary><p>会话是任务的工作来源；一个业务任务可以关联多个会话。</p>${detail.sessions.map(session=>`<div class="session-reference"><strong>${escape(session.agent)} · ${escape(session.machine_id)}</strong><code>${escape(session.session_id)}</code><span>${escape(session.repository || '')} ${escape(session.worktree || '')}</span><small>最近关联 ${when(session.last_seen_at)}</small><button class="reference-link" data-event="${escape(session.event_id)}">查看关联依据 ↗</button></div>`).join('') || '<p>尚未记录会话来源。后续由 Agent 在记录任务进展时附带会话标识。</p>'}</details><div class="task-tools"><label>交接给<select id="handoff-target"><option value="">其他 Agent</option><option value="codex">Codex</option><option value="claude">Claude Code</option><option value="kimi">Kimi</option><option value="dsh">DeepSeek</option></select></label><button class="button" data-task-tool="handoff">生成交接摘要</button><button class="button" data-task-tool="review">准备记忆审查</button></div><div id="task-tool-output" aria-live="polite"></div></section>`;
}
function handoffText(data) {
  const lines=[`任务：${data.task_id} · ${states[data.state] || data.state}`,`交接目标：${data.handoff?.to_agent || '其他 Agent'}（已准备，尚未派发）`,data.notice,''];
  lines.push('业务状态依据：');
  for(const head of data.heads) lines.push(`${head.event_id} · ${head.content.task_state} · ${head.content.what} · ${JSON.stringify(head.content.where)}`);
  if(!data.heads.length) lines.push('暂无明确生命周期依据。');
  lines.push('执行观测：',`${data.execution.state} · ${data.execution.observed_at || '未观测'} · ${data.execution.reason || ''}`,data.execution.notice);
  for(const observation of data.execution.sessions) lines.push(`${observation.session.agent} · ${observation.session.machine_id} · ${observation.session.session_id} · ${observation.state} · ${observation.observed_at} · ${observation.event_id} · ${observation.source || ''}`);
  lines.push('','当前仓库：');
  for(const repo of data.repositories) lines.push(`${repo.name} · ${repo.branch || '分支未知'} · ${repo.commit || '提交不可用'} · ${repo.dirty===null?'状态未知':repo.dirty?'有未提交改动':'干净'}`);
  for(const [title,items] of Object.entries(data.sections)) {
    if(!items.length) continue;
    lines.push('',title);
    for(const item of items) lines.push(`- ${item.title}\n  ${item.text}\n  依据：${item.event_id} · ${item.source || ''}`);
    if(data.omitted[title]) lines.push(`另 ${data.omitted[title]} 条未展开。`);
  }
  lines.push('','关联会话：');
  for(const session of data.sessions || []) lines.push(`${session.agent} · ${session.machine_id} · ${session.session_id}`);
  return lines.join('\n');
}
async function showTaskTool(kind, task, epoch) {
  const request=++toolEpoch;
  $('#task-tool-output').innerHTML='<p class="panel-hint">正在准备…</p>';
  try {
    const target=$('#handoff-target').value;
    const data=await api(kind,{task,...(kind==='handoff' && target?{to_agent:target}:{})});
    if(epoch!==renderEpoch || request!==toolEpoch) return;
    const text=kind==='handoff'?handoffText(data):JSON.stringify(data);
    $('#task-tool-output').innerHTML=`<div class="prepared-context"><strong>${kind==='handoff'?'交接摘要已准备，尚未派发':'审查材料已准备，尚未进行语义判断'}</strong><p>${kind==='handoff'?'复制到目标 Agent 即可继续；是否执行仍按任务授权。':'将材料交给当前 Agent，核对矛盾、重复和过期候选；审查建议须引用材料内的来源 ID。'}</p><button class="button" id="copy-task-context">复制材料</button><pre tabindex="0">${escape(text)}</pre><span id="copy-context-status" role="status"></span></div>`;
    $('#copy-task-context').addEventListener('click',async()=>{
      try {await navigator.clipboard.writeText(text);if(epoch===renderEpoch && request===toolEpoch) $('#copy-context-status').textContent='已复制';}
      catch {if(epoch===renderEpoch && request===toolEpoch) $('#copy-context-status').textContent='当前浏览器不支持复制，请选中上方文字复制。';}
    });
  } catch(error) {if(epoch===renderEpoch && request===toolEpoch) $('#task-tool-output').innerHTML=`<p class="inline-error">${escape(error.message)}</p>`;}
}
function serviceCards(items) {
  const names = {'claude-feishu':'Feishu Bridge','cluster-monitor':'集群资源采集'};
  return items.map(s => `<div class="service-card"><div class="service-top"><span class="service-icon">${icon(s.name === 'claude-feishu' ? 'memory' : 'runtime')}</span>${tag(s.running ? (s.ready ? '就绪' : '进程运行中') : '未运行', s.running && s.ready ? 'green' : '')}</div><h3>${escape(names[s.name] || s.name)}</h3><p>${s.running ? `PID ${s.pid} · ${s.name === 'claude-feishu' ? (s.ready ? 'WebSocket 已连接' : '连接未就绪') : s.name === 'cluster-monitor' ? '集群资源采集进程' : '本机监控进程'}` : s.configured ? '本机已有配置，当前进程未运行' : '本机未发现运行进程'}</p>${s.running ? `<p class="service-version">启动版本：${escape(s.code_at_start?.version || '未知（旧记录）')}<br><code>${escape(s.code_at_start?.commit?.slice(0,12) || '无启动提交记录')}</code><br>${s.code_changed === true ? '启动后源码已变化' : s.code_changed === false ? '与本机源码版本一致' : '版本差异待核对'}</p>` : ''}</div>`).join('');
}
const dayMs = 86400000;
const dateLabel = value => value ? new Intl.DateTimeFormat('zh-CN',{timeZone:'UTC',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value)) : '暂无时间记录';
const fold = value => String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN');
function timelineScale(range, mode) {
  if (!range?.start || !range?.end) return null;
  const first = new Date(range.start), last = new Date(range.end);
  const start = new Date(Date.UTC(first.getUTCFullYear(),first.getUTCMonth(),1));
  const end = new Date(Date.UTC(last.getUTCFullYear(),last.getUTCMonth()+1,1));
  const ticks=[];
  if (mode==='week') {
    start.setUTCDate(first.getUTCDate() - (first.getUTCDay()+6)%7);
    end.setTime(last.getTime()+7*dayMs);
    end.setUTCHours(0,0,0,0);
    const weeks=Math.ceil((end-start)/(7*dayMs)), step=Math.max(1,Math.ceil(weeks/260));
    for(let i=0;i<weeks;i+=step) {
      const at=new Date(start.getTime()+i*7*dayMs);
      ticks.push({at:at.getTime(),label:`${at.getUTCFullYear()}/${at.getUTCMonth()+1}/${at.getUTCDate()}`});
    }
  } else {
    const cursor=new Date(start), months=(end.getUTCFullYear()-start.getUTCFullYear())*12+end.getUTCMonth()-start.getUTCMonth();
    const step=Math.max(1,Math.ceil(months/(mode==='fit'?10:120)));
    for(let i=0;i<months;i+=step) {
      cursor.setTime(start.getTime()); cursor.setUTCMonth(start.getUTCMonth()+i);
      ticks.push({at:cursor.getTime(),label:`${cursor.getUTCFullYear()} / ${String(cursor.getUTCMonth()+1).padStart(2,'0')}`});
    }
  }
  const span=end-start, width=mode==='fit'?680:Math.max(680,ticks.length*(mode==='week'?110:140));
  return {start:start.getTime(),end:end.getTime(),span,width,ticks};
}
function gantt(rows, range, mode, project) {
  const scale=timelineScale(range,mode), pos=value=>scale?Math.max(0,Math.min(100,(new Date(value).getTime()-scale.start)/scale.span*100)):0;
  const ticks=scale?.ticks || [];
  const lines=ticks.map(t=>`<span class="gantt-grid-line" data-left="${pos(t.at)}"></span>`).join('');
  return `<section class="panel gantt-panel"><div class="gantt-scroll" tabindex="0" role="region" aria-label="${project?'任务':'项目'}甘特时间线，可横向滚动"><div class="gantt-grid" data-fit="${mode==='fit'}" data-timeline-width="${scale?.width || 680}"><div class="gantt-axis"><div class="gantt-axis-label">${project?'项目任务':'业务项目'}<small>记录跨度 · UTC</small></div><div class="gantt-ticks">${ticks.map(t=>`<span data-left="${pos(t.at)}">${escape(t.label)}</span>`).join('') || '<span>暂无时间记录</span>'}</div></div>${rows.map((row,i)=>{
    const url=project?href('projects',{name:project,task:row.task_id,scale:mode}):href('projects',{name:row.name,scale:mode});
    const dated=!!(row.first_recorded_at && row.last_recorded_at);
    const left=dated?pos(row.first_recorded_at):0, width=dated?Math.max(0.7,pos(row.last_recorded_at)-left):0;
    const dateText=dated?`${dateLabel(row.first_recorded_at)} → ${dateLabel(row.last_recorded_at)}`:'暂无时间记录';
    return `<div class="gantt-row ${row.selected?'selected':''}"><div class="gantt-label"><a href="${url}"><strong>${escape(row.title || row.name)}</strong><small>${project?escape(row.task_id):`${row.task_count} 项任务 · ${row.documents.length} 份文档`}</small></a>${project?`<div class="task-state-pair">${taskTag(row.state)}${executionTag(row.execution)}</div>`:''}</div><div class="gantt-track">${lines}${dated?`<a class="gantt-bar ${project?'state-'+row.state:'tone-'+i%4} ${width<12?'short-bar':''}" data-left="${left}" data-width="${width}" href="${url}" aria-label="${escape(row.title)}，${escape(dateText)}" title="${escape(row.title)} · ${escape(dateText)}"><span class="gantt-date">${escape(dateText)}</span><span class="gantt-milestone" aria-hidden="true"></span></a>`:'<span class="gantt-empty">暂无时间记录</span>'}</div></div>`;
  }).join('')}</div></div></section>`;
}
function positionGantt() {
  document.querySelectorAll('[data-left]').forEach(el=>el.style.left=`${Number(el.dataset.left)}%`);
  document.querySelectorAll('[data-width]').forEach(el=>el.style.width=`${Number(el.dataset.width)}%`);
  document.querySelectorAll('[data-timeline-width]').forEach(el=>{
    const labelWidth=parseFloat(getComputedStyle(el).getPropertyValue('--label-width')) || 240;
    const width=el.dataset.fit==='true'?Math.max(160,Math.floor(el.parentElement.clientWidth-labelWidth)):Number(el.dataset.timelineWidth);
    el.style.setProperty('--timeline-width',`${width}px`);
    const ticks=[...el.querySelectorAll('.gantt-ticks>span')], count=Math.max(2,Math.floor(width/85));
    const step=Math.max(1,Math.ceil((ticks.length-1)/(count-1)));
    ticks.forEach((tick,i)=>{tick.hidden=i%step!==0 && i!==ticks.length-1;tick.style.transform=i===ticks.length-1 && ticks.length>1?'translateX(-100%)':'none';});
  });
}
function scaleControls(mode, params) {
  return `<div class="journey-controls" role="group" aria-label="时间线刻度">${[['fit','全部历程'],['month','按月'],['week','按周']].map(([value,label])=>`<a class="button ${mode===value?'selected':''}" aria-pressed="${mode===value}" href="${href('projects',{...Object.fromEntries(params),scale:value})}">${label}</a>`).join('')}</div>`;
}
function milestoneRows(items) {
  if(!items.length) return '<p class="panel-hint">暂无关键节点。</p>';
  return `<ol class="milestone-list">${items.map(item=>`<li class="milestone-item"><time datetime="${escape(item.created_at)}">${dateLabel(item.created_at)}</time><span class="milestone-marker ${item.event_type}" aria-hidden="true"></span><div class="milestone-body"><div class="detail-tags">${tag(item.task_state?states[item.task_state]:(types[item.event_type] || item.event_type),item.event_type==='risk'?'amber':'')}${item.is_historical?tag('历史节点'):''}</div><h3>${escape(item.title)}</h3><p>${escape(item.excerpt.length>120?item.excerpt.slice(0,120)+'…':item.excerpt)}</p><details class="milestone-evidence"><summary>查看依据</summary><p>${escape(item.task_id && item.task_id!=='-'?item.task_id:'项目记录')} · ${escape(item.projects.join(' / '))}</p><button class="text-button" data-event="${escape(item.event_id)}">展开来源与验证 ↗</button></details></div></li>`).join('')}</ol>`;
}
async function showProjects(params, epoch) {
  const name=params.get('name'), task=params.get('task');
  const mode=['fit','month','week'].includes(params.get('scale'))?params.get('scale'):'fit';
  const data=await api('timeline',{...(name?{project:name}:{}),...(name&&task?{task}: {})});
  if(epoch !== renderEpoch) return;
  $('#last-read').textContent=`项目采样 ${when(data.sampled_at)} · 手动刷新`;
  if(!name) {
    const query=params.get('q') || '', state=params.get('state') || '';
    const projects=data.projects.filter(p=>fold([p.title,p.name,p.description,...p.repositories].join(' ')).includes(fold(query)) && (!state || p.task_counts[state]>0));
    const totals=data.task_counts;
    content.innerHTML=heading('PROJECT JOURNEYS','把项目，放回时间里。','从最早记录到最近进展，沿着时间看每个项目如何走到今天。')+
      `<div class="journey-metrics"><div class="journey-metric"><small>${data.project_mode==='knowledge'?'业务项目':'来源仓库'}</small><strong>${data.projects.length}</strong></div><div class="journey-metric"><small>进行中任务</small><strong>${totals.active || 0}</strong></div><div class="journey-metric"><small>已关闭任务</small><strong>${totals.closed || 0}</strong></div><div class="journey-metric"><small>记录覆盖</small><strong class="journey-date-range">${dateLabel(data.range?.start)}<span>— ${dateLabel(data.range?.end)}</span></strong></div></div>`+
      `<div class="journey-stat-scope">当前全部项目口径 · 按任务 ID 去重 · 未声明 ${totals.unknown || 0} · 暂停 ${totals.paused || 0} · 冲突 ${totals.conflict || 0}<br>已关联 ${number(data.coverage.associated_events)} / ${number(data.coverage.total_events)} 条记录；未归属 ${number(data.coverage.unassigned_events)} 条记录、${number(data.coverage.unassigned_tasks)} 项任务。记录日期表示证据覆盖范围。</div>`+
      attentionPanel(data)+
      (data.coverage.unassigned_task_items.length?`<details class="unassigned-tasks"><summary>查看未归属任务（前 ${data.coverage.unassigned_task_items.length} 项）</summary>${data.coverage.unassigned_task_items.map(t=>`<div><code>${escape(t.task_id)}</code>${taskTag(t.state)}</div>`).join('')}<p>由 Agent 记录明确业务项目归属后，相关任务会进入项目时间线。</p></details>`:'')+
      `<div class="journey-toolbar"><form class="journey-search" id="project-search">${icon('search')}<input aria-label="筛选项目" name="q" placeholder="找到一个项目…" value="${escape(query)}"><select name="state" aria-label="按关联任务状态筛选"><option value="">全部项目</option>${Object.entries(states).map(([key,label])=>`<option value="${key}" ${state===key?'selected':''}>含${escape(label)}任务</option>`).join('')}</select><button class="button" type="submit">筛选</button></form>${scaleControls(mode,params)}</div>`+
      (projects.length?gantt(projects,data.range,mode):empty('没有匹配的项目'))+
      `<p class="journey-note">时间条表示首条至最近一条已记录活动，不等于实际工期；点击项目查看任务历程与文档。</p>${data.project_mode!=='knowledge'?'<p class="journey-note">当前尚无项目知识，暂按来源仓库展示；整理项目知识后可聚合跨仓工作。</p>':''}`;
    $('#project-search').addEventListener('submit',e=>{e.preventDefault();location.hash=href('projects',{...Object.fromEntries(new FormData(e.currentTarget)),scale:mode});});
    $('#project-search select').addEventListener('change',()=>$('#project-search').requestSubmit());
  } else {
    const project=data.projects[0], tasks=project.tasks || [], items=project.milestones || [];
    const selected=tasks.find(t=>t.task_id===task);
    const counts=project.task_counts;
    content.innerHTML=heading('PROJECT JOURNEY',escape(project.title),escape(project.description || '沿时间回顾这项工作的关键节点。'),`<a class="button" href="#/projects">← 全部项目</a>`)+
      `<div class="journey-project-links">${project.home?`<a class="button" href="${href('documents',{business:name,path:project.home})}">${icon('documents')}项目知识</a>`:''}<a class="button" href="${href('documents',project.kind==='knowledge'?{business:name}:{all:'true'})}">项目文档 · ${project.documents.length}</a>${freshnessTag(project.freshness)}<span>${dateLabel(project.first_recorded_at)} → ${dateLabel(project.last_recorded_at)}</span></div>`+
      `<div class="journey-toolbar"><div class="journey-legend">${Object.entries(states).filter(([key])=>counts[key]).map(([key,label])=>`<span><i class="legend-dot ${key}"></i>${label} ${counts[key]}</span>`).join('') || '<span>尚无已关联任务</span>'}</div>${scaleControls(mode,params)}</div>`+
      (tasks.length?`<details class="journey-task-board" ${task || tasks.length<=6?'open':''}><summary>任务时间线 · ${tasks.length} 项<span>按需查看各项工作的记录跨度</span></summary><form class="journey-task-picker" id="task-picker"><label>聚焦任务<select name="task" aria-label="聚焦任务"><option value="">整个项目</option>${tasks.map(t=>`<option value="${escape(t.task_id)}" ${t.task_id===task?'selected':''}>${escape(t.title)} · ${escape(states[t.state])}</option>`).join('')}</select></label></form>${gantt(tasks.map(t=>({...t,selected:t.task_id===task})),{start:project.first_recorded_at,end:project.last_recorded_at},mode,name)}</details>`:'<div class="panel panel-hint">尚无任务划分，可先阅读项目知识和下方关键节点。</div>')+
      `<p class="journey-note">条形表示任务记录跨度；业务状态与 Agent 执行观测分别记录，进行中不代表进程正在运行。</p>`+
      (task?taskContext(data.task_detail):attentionPanel(data,name))+
      `<div class="journey-detail-grid"><section class="panel"><div class="panel-heading journey-section-heading"><h2>${selected?'项目内任务历程':'项目历程'}</h2>${task?`<a href="${href('projects',{name,scale:mode})}">查看整个项目 →</a>`:`<span>关键节点节选</span>`}</div>${selected?`<div class="journey-task-filter"><strong>${escape(selected.title)}</strong>${taskTag(selected.state)}<small>${escape(task)}</small></div>`:''}${items.length>10?milestoneRows(items.slice(0,3))+`<details class="journey-more"><summary>展开中间 ${items.length-6} 个关键节点</summary>${milestoneRows(items.slice(3,-3))}</details>`+milestoneRows(items.slice(-3)):milestoneRows(items) || '<p class="panel-hint">暂无关键节点。</p>'}${project.milestone_omitted?`<p class="journey-note">展示覆盖首尾的 ${items.length} 个关键节点，另 ${project.milestone_omitted} 个未展开；点击任务时间条可聚焦单项工作。</p>`:''}</section><aside class="journey-task-summary panel"><div class="panel-heading"><h2>项目脉络</h2></div><div class="panel-hint"><strong>最早记录</strong><p>${dateLabel(project.first_recorded_at)}</p><strong>最近记录</strong><p>${dateLabel(project.last_recorded_at)}</p><strong>关联任务</strong><p>${tasks.length} 项 · ${counts.active || 0} 项进行中</p><strong>来源仓库</strong><p>${escape(project.repositories.join('、') || '暂无来源仓库')}</p><strong>阅读入口</strong><p>项目知识保留完整叙述，时间线聚焦决策、验证、修复与状态变化。</p></div></aside></div>`;
  }
  $('.journey-task-board')?.addEventListener('toggle',positionGantt);
  $('#task-picker select')?.addEventListener('change',e=>{location.hash=href('projects',{name,scale:mode,task:e.target.value});});
  document.querySelectorAll('[data-task-tool]').forEach(button=>button.addEventListener('click',()=>showTaskTool(button.dataset.taskTool,task,epoch)));
  positionGantt();
  if(data.errors.length) content.insertAdjacentHTML('afterbegin','<p class="data-warning">部分来源数据无效，项目历程可能不完整；请在监控的本机服务中查看工作区自检。</p>');
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
  content.innerHTML = heading('KNOWLEDGE LIBRARY',escape(project?.title || '全部文档'),project ? '围绕项目阅读；文档保留原有路径和来源。' : '通用说明和未归属文档也可在这里查找。',`<a class="button" href="#/documents">← 按项目阅读</a>`) + `<div class="document-layout"><aside class="document-list"><form id="document-filter" class="document-search">${icon('search')}<input aria-label="筛选文档" name="q" placeholder="搜索标题、路径或正文…" value="${escape(query)}"></form><div id="document-links">${docs.map(d=>`<a href="${href('documents',{...context,path:d.path,q:query})}" class="document-link ${path === d.path ? 'selected' : ''}">${icon('documents')}<span><strong>${escape(d.title)}</strong><small>${escape(d.path)}</small>${freshnessTag(d.freshness)}${d.excerpt ? `<small class="document-excerpt">${escape(d.excerpt)}</small>` : ''}</span><span class="file-kind">${d.kind.toUpperCase()}</span></a>`).join('') || empty('没有匹配的文档')}</div></aside><section class="document-preview panel" id="document-preview"><div class="document-placeholder">${icon('documents')}<h2>让知识触手可及。</h2><p>选择一份文档，开始阅读。</p><span>${number(data.items.length)} 份 Markdown / SVG 文档</span></div></section></div>`;
  $('#document-filter').addEventListener('submit',e=>{e.preventDefault();location.hash=href('documents',{...context,q:new FormData(e.currentTarget).get('q'),path});});
  if (!path) return;
  const doc = await api('document',{path});
  if (epoch !== renderEpoch) return;
  $('#document-preview').innerHTML = `<div class="document-toolbar"><span>${escape(path)}</span>${doc.updated_at ? `<time>文件更新 ${when(doc.updated_at)}</time>` : ''}</div>${freshnessPanel(doc.freshness)}${doc.kind === 'svg' ? `<iframe class="svg-preview" title="SVG 文档预览" sandbox src="${escape(doc.asset)}"></iframe>` : `<article class="prose">${doc.html}</article>`}`;
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
  const tab=['cluster','train','services'].includes(params.get('tab'))?params.get('tab'):'cluster';
  const titles={cluster:['CLUSTER RESOURCES','集群资源。','每台机器、每张 GPU，以及已配置入口的采集快照。'],train:['TRAINING & EVALUATION','训练与评测。','读取已配置监控的本机快照，按需手动刷新。'],services:['LOCAL SERVICES','本机服务。','核对服务进程、启动版本与工作区健康状态。']};
  content.innerHTML=`<div class="runtime-view">${heading(...titles[tab])}<nav class="runtime-tabs" aria-label="监控分类">${[['cluster','集群资源'],['train','训练与评测'],['services','本机服务']].map(([value,label])=>`<a class="${tab===value?'selected':''}" href="${href('runtime',{tab:value})}" ${tab===value?'aria-current="page"':''}>${label}</a>`).join('')}</nav><div class="runtime-content" id="runtime-content"></div></div>`;
  if(tab==='cluster') {
    runtimeCleanup=mountCluster($('#runtime-content'),{isCurrent:()=>epoch===renderEpoch,onRead:data=>{
      if(epoch!==renderEpoch) return;
      const sampled=data.sampled_at?new Date(data.sampled_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'暂无采样';
      $('#last-read').textContent=`集群采样 ${sampled}（北京时间） · ${data.collector_running?'每 3 秒读取快照':'采集未运行'}`;
    }});
    return;
  }
  if(tab==='services') return showLocalServices(epoch);
  const monitors=await api('monitors');
  if(epoch!==renderEpoch) return;
  $('#runtime-content').innerHTML=`<div class="runtime-scope"><span>范围：本机 adapter 与配置</span><span>手动读取 · 不启停训练或评测</span></div><div class="monitor-grid">${monitors.items.length?monitors.items.map((item,index)=>`<section class="panel monitor-panel"><div class="panel-heading"><h2>${escape(item.name)}</h2><span>WATCHDOG</span></div><div id="monitor-${index}" class="loading">正在读取已配置监控…</div></section>`).join(''):'<section class="panel monitor-panel"><h2>尚未安装监控 adapter</h2><p>在本机 watchdog/adapters 目录添加 adapter，并填写对应的私有配置。</p></section>'}</div>`;
  await Promise.all(monitors.items.map(async (item,index)=>{
    try {
      const data=await api('monitor',{kind:item.name});
      if(epoch!==renderEpoch) return;
      const panel=$(`#monitor-${index}`);
      panel.className='monitor-content';
      panel.innerHTML=data.error?`<div class="inline-error">${escape(data.error)}</div>`:!data.configured?`<div class="monitor-empty"><span class="muted-dot status-dot"></span><h3>尚未配置</h3><p>接入现有 watchdog 配置后，可在这里读取状态。</p></div>`:`<pre class="monitor-status">${escape(data.status || '本次采集未返回活跃任务详情。')}</pre>`;
      panel.insertAdjacentHTML('beforeend',`<div class="sample-meta"><span>范围：本机 · ${escape({available:'有采集结果',unconfigured:'未配置',unavailable:'不可用',no_data:'无数据'}[data.state] || '状态未知')}</span><span>采样时间：${when(data.sampled_at)}</span><span>来源：${escape(data.collector)}</span><code>${escape(data.source)}</code></div>`);
    } catch(error) {
      if(epoch===renderEpoch) {$(`#monitor-${index}`).className='monitor-content';$(`#monitor-${index}`).innerHTML=`<div class="inline-error">${escape(error.message)}</div>`;}
    }
  }));
  if(epoch===renderEpoch) $('#last-read').textContent='训练与评测 · 本机只读快照 · 手动刷新';
}
async function showLocalServices(epoch) {
  const services=await api('services');
  if(epoch!==renderEpoch) return;
  $('#last-read').textContent=`本机服务采样 ${when(services.sampled_at)} · 手动刷新`;
  $('#runtime-content').innerHTML=`<div class="runtime-scope"><span>范围：本机</span><span>工具核心 ${escape(services.installed_code.version || '版本未知')} · ${escape(services.installed_code.commit?.slice(0,12) || '提交未知')}</span><span>来源：本机进程核验与服务 PID 记录</span><span>采样时间：${when(services.sampled_at)}</span></div><div class="services-grid">${serviceCards(services.items)}</div><div class="section-heading"><h2>记忆来源机器</h2><span class="muted">最后记录时间，不是在线探测</span></div><div class="machines-grid">${overview.machines.map(m=>`<div class="machine-card"><span class="machine-symbol">${icon('overview')}</span><strong>${escape(m.name)}</strong><span>最后事件 ${when(m.last_event_at)}</span></div>`).join('')}</div><section class="panel doctor-panel"><div class="panel-heading"><h2>工作区自检</h2><span>本机只读 · 不连接远端</span></div><div id="doctor-results" class="loading">正在检查配置、记忆与链接…</div></section>`;
  try {
    const data=await api('doctor');
    if(epoch!==renderEpoch) return;
    $('#doctor-results').className='doctor-checks';
    $('#doctor-results').innerHTML=data.checks.map(c=>`<div class="doctor-check">${tag({ok:'通过',warning:'待核对',error:'有错误',unconfigured:'未配置'}[c.state],c.state==='ok'?'green':c.state==='error'||c.state==='warning'?'amber':'')}<div><strong>${escape(c.name)}</strong><p>${escape(c.detail)}</p>${c.hint?`<small>${escape(c.hint)}</small>`:''}</div></div>`).join('')+doctorDetails(data)+`<p class="panel-hint">${escape(data.notice)} · ${when(data.sampled_at)}<br>本机状态来源：${escape(data.local_root)}<br>各机器最后记忆时间见上方列表。</p>`;
  } catch(error) {
    if(epoch===renderEpoch) $('#doctor-results').innerHTML=`<p class="inline-error">${escape(error.message)}</p>`;
  }
}
async function openEvent(id) {
  const dialog=$('#event-dialog'), epoch=++eventEpoch;
  $('#event-detail').innerHTML='<div class="loading">正在读取节点依据…</div>';
  dialog.scrollTop=0;
  if(!dialog.open) dialog.showModal();
  try {
    const data=await api('event',{id}), e=data.event;
    if(epoch !== eventEpoch || !dialog.open) return;
    const fields=[['内容','what'],['原因与背景','why'],['来源位置','where'],['验证依据','verified_by']];
    $('#event-detail').innerHTML=`<div class="eyebrow">PROJECT EVIDENCE</div><div class="detail-tags">${tag(types[e.event_type] || e.event_type)}${data.replaced ? tag(e.content.task_state?'有后续状态':'有后续修正','amber') : ''}</div><h2 id="drawer-title">${escape(e.title)}</h2><div class="detail-meta">${new Date(e.created_at).toLocaleString('zh-CN',{timeZone:'UTC'})} UTC · ${escape(e.agent)} · ${escape(e.machine_id)}</div>${fields.filter(([,key])=>e.content[key]?.length).map(([title,key])=>`<section class="detail-section"><h3>${title}</h3><p>${escape(Array.isArray(e.content[key]) ? e.content[key].join('\n') : e.content[key])}</p></section>`).join('')}${data.replaced_by.length ? `<section class="detail-section"><h3>${e.content.task_state?'后续状态依据':'后续修正依据'}</h3>${data.replaced_by.map(key=>`<button class="reference-link" data-event="${escape(key)}">${e.content.task_state?'打开后续状态':'打开修正记录'} ↗</button>`).join('')}</section>` : ''}<details class="detail-section"><summary>来源标识</summary><p><code>${escape(e.event_id)}</code><br><code>${escape(data.source)}</code></p></details>`;
  } catch(error) { if(epoch === eventEpoch && dialog.open) $('#event-detail').innerHTML=`<p class="inline-error">${escape(error.message)}</p>`; }
}
async function render() {
  runtimeCleanup();runtimeCleanup=()=>{};
  const epoch=++renderEpoch, {name,params}=route();
  ++eventEpoch;
  window.scrollTo(0,0);
  document.body.dataset.view=name;
  $('#event-dialog').close();
  $('#breadcrumb').textContent=labels[name];
  $('#global-search input').value = name === 'projects' ? (params.get('q') || '') : '';
  document.title=`${labels[name]} · Backstage`;
  document.querySelectorAll('[data-nav]').forEach(a=>{a.classList.toggle('active',a.dataset.nav===name);if(a.dataset.nav===name)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
  content.innerHTML='<div class="loading">正在读取工作区…</div>';
  try {
    overview = await api('overview');
    if(epoch !== renderEpoch) return;
    if(!params.get('name') && params.get('task')) { const projects=overview.tasks.find(t=>t.task_id===params.get('task'))?.business_projects || []; if(projects.length===1) params.set('name',projects[0]); }
    $('#workspace-name').textContent=overview.workspace;
    $('#machine-name').textContent=overview.machine_id;
    $('#project-count').textContent=overview.projects.length;
    $('#version').textContent=overview.version ? `v${overview.version}` : 'Backstage';
    $('#last-read').textContent=`数据采样 ${when(overview.sampled_at)} · 快照最多复用 ${overview.cache_seconds} 秒`;
    await ({projects:()=>showProjects(params,epoch),documents:()=>showDocuments(params,epoch),runtime:()=>showRuntime(params,epoch)})[name]();
    if(epoch === renderEpoch && overview.errors.length) content.insertAdjacentHTML('afterbegin',`<div class="data-warning">工作区有 ${overview.errors.length} 项数据校验问题；当前展示有效记录，任务统计可能不完整。请运行 memory validate / audit 核对。</div>`);
  } catch(error) { if(epoch === renderEpoch) content.innerHTML=`<div class="error-page"><h1>暂时无法读取</h1><p>${escape(error.message)}</p><p>检查工作区配置后，点击右上角刷新。</p></div>`; }
}
document.addEventListener('click',e=>{const row=e.target.closest('[data-event]');if(row)openEvent(row.dataset.event);});
$('#close-drawer').addEventListener('click',()=>$('#event-dialog').close());
$('#event-dialog').addEventListener('click',e=>{if(e.target===$('#event-dialog'))$('#event-dialog').close();});
$('#global-search').addEventListener('submit',e=>{e.preventDefault();location.hash=href('projects',{q:$('input',e.currentTarget).value});});
document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();$('#global-search input').focus();}});
$('#refresh').addEventListener('click',()=>{overview=null;render();});
window.addEventListener('hashchange',render);
window.addEventListener('pagehide',()=>runtimeCleanup());
window.addEventListener('pageshow',event=>{if(event.persisted) render();});
window.addEventListener('resize',positionGantt);
render();
