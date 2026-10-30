// This view only reads the collector's saved snapshot. It never starts collection.
import {api} from './api.js';
const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const numeric = value => typeof value === 'number' && Number.isFinite(value);
const fixed = (value, digits=1) => numeric(value) ? value.toFixed(digits) : '—';
const percent = value => numeric(value) ? `${value.toFixed(1).replace(/\.0$/, '')}%` : '—';
const gib = value => numeric(value) ? (value/1024).toFixed(value>=102400 ? 0 : 1) : '—';
const clamp = value => numeric(value) ? Math.max(0, Math.min(100, value)) : 0;
const mean = values => values.length ? values.reduce((a,b)=>a+b,0)/values.length : null;
const date = value => {
  if(value == null || value === '') return '—';
  const parsed = new Date(typeof value === 'number' ? value*1000 : value);
  return Number.isNaN(parsed.getTime()) ? '—' : new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(parsed);
};
const rate = value => {
  if(!numeric(value)) return '—';
  if(value>=1024*1024) return `${(value/1024/1024).toFixed(1)} MB/s`;
  if(value>=1024) return `${(value/1024).toFixed(1)} KB/s`;
  return `${Math.round(value)} B/s`;
};
const since = value => {
  if(!numeric(value)) return '尚未观测';
  const seconds=Math.max(0,Date.now()/1000-value);
  if(seconds<10) return '刚刚';
  if(seconds<60) return `${Math.floor(seconds)} 秒前`;
  if(seconds<3600) return `${Math.floor(seconds/60)} 分钟前`;
  return `${Math.floor(seconds/3600)} 小时前`;
};
const meter = value => `<div class="cluster-bar"><i data-meter="${clamp(value)}"></i></div>`;
const metric = (label,text,value) => `<div class="cluster-metric"><div><span>${label}</span><strong>${text}</strong></div>${meter(value)}</div>`;
const stat = (label,value,unit,foot,ratio=null) => `<article class="cluster-stat"><span>${label}</span><strong>${value}${unit?`<small>${unit}</small>`:''}</strong>${numeric(ratio)?meter(ratio):''}<p>${foot}</p></article>`;
const heat = value => !numeric(value) ? 0 : value<5 ? 1 : value<30 ? 2 : value<65 ? 3 : value<90 ? 4 : 5;
const shortGPU = value => String(value || 'GPU').replace(/^NVIDIA\s+/i,'').replace(/^GeForce\s+/i,'').trim();
const fontScales = [80,100,120,140];
function savedFontScale() {
  try {
    const value=Number(localStorage.getItem('backstage.clusterFontScale'));
    return fontScales.includes(value)?value:100;
  } catch { return 100; }
}

export function mountCluster(target, {isCurrent=()=>true,onRead=()=>{}}={}) {
  let data=null, filter='', search='', paused=false, pending=false, closed=false, request=0, controller=null, failed=false;
  const current=()=>!closed && isCurrent();
  const $=selector=>target.querySelector(selector);
  target.innerHTML=`<div class="cluster-monitor"><div class="cluster-toolbar"><div class="cluster-connection" role="status" id="cluster-connection">正在读取集群快照…</div><div class="cluster-actions"><label class="cluster-font-control" for="cluster-font-scale">字号<select id="cluster-font-scale" aria-label="集群字号缩放">${fontScales.map(scale=>`<option value="${scale}">${scale}%</option>`).join('')}</select></label><button class="button" data-cluster-pause aria-pressed="false">暂停画面</button><button class="button" data-cluster-refresh>读取最新快照</button></div></div><div class="cluster-provenance" id="cluster-provenance"></div><div id="cluster-notice"></div><div id="cluster-summary" class="cluster-summary"></div><section id="cluster-resources" hidden><div class="cluster-filterbar"><div class="cluster-filters" id="cluster-filters" role="group" aria-label="机器类型筛选"></div><label class="cluster-search"><span aria-hidden="true">⌕</span><input id="cluster-search" type="search" aria-label="搜索集群机器" placeholder="搜索机器、分组或 IP"></label></div><div class="cluster-caption"><span>整机资源与平均功耗</span><span>逐卡：GPU 近 60 秒均值 / 显存</span></div><div id="cluster-nodes" class="cluster-nodes"></div><p class="cluster-note">GPU 利用率为近 60 秒窗口均值，峰值见卡片提示；活跃卡按窗口峰值 ≥ 10% 统计。过期节点不计入采集时的整体利用率。</p></section><details id="cluster-ports" class="cluster-ports" hidden><summary>端口健康与流量来源<span>按需查看已配置的入口观测</span></summary><div id="cluster-port-content"></div></details></div>`;
  const fontRoot=$('.cluster-monitor');
  function applyFontScale(value) {
    const selected=fontScales.includes(value)?value:100;
    fontRoot.style.setProperty('--cluster-font-scale',String(selected/100));
    $('#cluster-font-scale').value=String(selected);
  }
  applyFontScale(savedFontScale());
  function change(event) {
    if(event.target.id!=='cluster-font-scale') return;
    const selected=Number(event.target.value);
    applyFontScale(selected);
    try { localStorage.setItem('backstage.clusterFontScale',String(selected)); } catch {}
  }

  function fresh() {
    return !!data && data.state==='available' && data.collector_running && data.snapshot?.state!=='stopped' && !failed && !paused;
  }
  function applyMeters() {
    target.querySelectorAll('[data-meter]').forEach(el=>{el.style.width=`${clamp(Number(el.dataset.meter))}%`;});
  }
  function renderStatus() {
    if(!current()) return;
    const stopped=data?.snapshot && (!data.collector_running || data.snapshot.state==='stopped');
    const state=paused?'paused':failed?'failed':stopped?'stopped':data?.state==='no_data' && !data.collector_running?'idle':data?.state || 'loading';
    const labels={paused:'画面已暂停 · 保留当前采样',failed:'读取失败 · 保留上次快照',stopped:'采集已停止 · 保留已有快照',idle:'采集未运行 · 尚无采样',available:'采集中 · 自动读取快照',stale:'采样已过期',unconfigured:'尚未配置集群',unavailable:'采集暂不可用',no_data:'等待首次采样',loading:'正在读取集群快照…'};
    $('#cluster-connection').className=`cluster-connection ${state==='available'?'available':state}`;
    const status=data?.state!=='available' && typeof data?.status==='string'?data.status:'';
    $('#cluster-connection').textContent=!paused && !failed && !stopped && status?status:labels[state] || '采集状态待核对';
    $('[data-cluster-pause]').textContent=paused?'继续刷新':'暂停画面';
    $('[data-cluster-pause]').setAttribute('aria-pressed',String(paused));
    $('[data-cluster-refresh]').disabled=pending;
    if(data) $('#cluster-provenance').innerHTML=`<span>采集：${date(data.sampled_at)}</span><span>页面读取：${date(data.read_at)}</span><span>北京时间 · UTC+8</span><span>页面每 3 秒读取${data.snapshot?.interval?` · 采集周期 ${escape(data.snapshot.interval)} 秒`:''}</span><span class="cluster-source">来源：${escape(data.source || '集群采集快照')}</span>`;
    target.querySelector('.cluster-monitor').classList.toggle('is-stale',!!data && !fresh());
  }
  function renderNodes() {
    if(!current() || !data?.snapshot) return;
    const nodes=(data.snapshot.nodes || []).filter(node=>(!filter || node.group===filter) && [node.alias,node.label,node.address,node.group].join(' ').toLocaleLowerCase('zh-CN').includes(search));
    $('#cluster-nodes').innerHTML=nodes.length?nodes.map(node=>{
      const sample=node.sample, gpus=Array.isArray(sample?.gpus)?sample.gpus:[];
      const windowed=sample?.gpu_sampling==='loop', single=sample?.gpu_sampling==='single';
      const sampling=windowed?'近 60 秒均值':single?'单次瞬时值':'利用率（采样方式未声明）';
      const state=node.status==='online' && !fresh()?'stale':node.status;
      const status=({online:'在线',connecting:'连接中',stale:'旧读数',offline:'离线'})[state] || '状态未知';
      const gpuPercent=mean(gpus.map(gpu=>gpu.utilization).filter(numeric));
      const watts=mean(gpus.map(gpu=>gpu.power_watts).filter(numeric)), limit=mean(gpus.map(gpu=>gpu.power_limit_watts).filter(numeric));
      const power=numeric(watts)?`${fixed(watts,0)} / ${fixed(limit,0)} W`:'—';
      const resources=sample?`${escape(shortGPU(gpus[0]?.name))} × ${gpus.length || '—'} · ${gib(gpus[0]?.memory_total_mib)} GiB/卡 · 内存 ${numeric(sample.memory?.total)?fixed(sample.memory.total/1024**3,0):'—'} GiB · ${numeric(sample.cpu_count)?sample.cpu_count:'—'} 核`:'等待机器首批采样';
      const tiles=gpus.map(gpu=>{
        const memory=numeric(gpu.memory_used_mib) && numeric(gpu.memory_total_mib) && gpu.memory_total_mib>0;
        const peak=windowed?`60 秒峰值 ${percent(gpu.utilization_peak)}`:single?'单次值 · 峰值未知':'采样方式未声明';
        const description=`GPU ${gpu.index} · ${sampling} ${percent(gpu.utilization)} · ${peak} · 显存 ${gib(gpu.memory_used_mib)}/${gib(gpu.memory_total_mib)} GiB · ${fixed(gpu.temperature,0)} °C · ${fixed(gpu.power_watts,0)} W`;
        return `<div class="cluster-gpu heat-${heat(gpu.utilization)}" title="${escape(description)}"><div class="cluster-gpu-top"><span>GPU ${escape(gpu.index)}</span><strong>${percent(gpu.utilization)}</strong></div>${meter(gpu.utilization)}<div class="cluster-gpu-memory">${memory?`${gib(gpu.memory_used_mib)}<span> / ${gib(gpu.memory_total_mib)} GiB</span>`:'显存 —'}</div>${meter(memory?100*gpu.memory_used_mib/gpu.memory_total_mib:null)}<div class="cluster-gpu-peak">${peak}</div></div>`;
      }).join('');
      return `<article class="cluster-node ${state==='online'?'online':state==='stale'?'stale':state==='offline'?'offline':'connecting'}"><div class="cluster-node-head"><div class="cluster-node-name"><strong>${escape(node.label || node.alias)}</strong>${node.label && node.label!==node.alias?`<span>${escape(node.alias)}</span>`:''}<code>${escape(node.address)}</code><span class="cluster-node-status">${status}</span></div><p class="cluster-node-resources" title="${escape(sample?.cpu_model)}">${resources}</p><div class="cluster-node-metrics">${metric('CPU',percent(sample?.cpu_percent),sample?.cpu_percent)}${metric('内存',percent(sample?.memory?.percent),sample?.memory?.percent)}${metric(windowed?'GPU 60 秒均值':single?'GPU 瞬时值':'GPU 利用率',percent(gpuPercent),gpuPercent)}${metric('平均功耗 / 上限',power,numeric(watts)&&numeric(limit)&&limit>0?100*watts/limit:null)}</div></div>${gpus.length?`<div class="cluster-gpus">${tiles}</div>`:'<p class="cluster-node-empty">尚无 GPU 读数</p>'}<div class="cluster-node-foot"><span>机器采样 ${date(sample?.sampled_at)}</span><span>GPU ${sampling}</span>${node.error || sample?.gpu_error?`<span class="cluster-error">${escape(node.error || sample.gpu_error)}</span>`:''}</div></article>`;
    }).join(''):'<div class="cluster-empty"><strong>没有匹配的机器</strong><p>调整类型筛选或搜索词。</p></div>';
    applyMeters();
  }
  function renderPorts() {
    const ports=data?.snapshot?.ports;
    $('#cluster-ports').hidden=!ports;
    if(!ports) return;
    const checks=Array.isArray(ports.checks)?ports.checks:[], traffic=ports.traffic;
    const trafficFresh=fresh() && !ports.traffic_stale && !!traffic;
    const socketsFresh=trafficFresh && !traffic.socket_error;
    const logFresh=trafficFresh && traffic.bridge?.log_available;
    const sources=Array.isArray(traffic?.sources)?traffic.sources:[];
    const now=Date.now()/1000, interval=data.snapshot.health_interval;
    const maxAge=numeric(interval) && interval>0?interval*3+5:null;
    const evaluated=checks.map(check=>{
      if(!numeric(check.checked_at)) return {check,status:'pending'};
      const recent=fresh() && maxAge!==null && check.checked_at<=now+5 && now-check.checked_at<=maxAge;
      return {check,status:recent && ['up','down'].includes(check.status)?check.status:recent?'pending':'stale'};
    });
    const valid=evaluated.filter(item=>item.status==='up' || item.status==='down').length;
    const healthy=evaluated.filter(item=>item.status==='up').length;
    const stale=evaluated.filter(item=>item.status==='stale').length;
    const waiting=evaluated.filter(item=>item.status==='pending').length;
    const times=checks.map(check=>check.checked_at).filter(numeric), latest=times.length?Math.max(...times):null;
    const summary=stat('当前健康端口',valid?healthy:'—',`/ ${checks.length}`,`有效 ${valid} 项 · 过期 ${stale} 项 · 待检 ${waiting} 项`)+
      stat('外部连接',socketsFresh && numeric(traffic.connections)?traffic.connections:'—','',socketsFresh?`${sources.filter(source=>source.connections>0).length} 个来源 IP`:'等待新鲜 TCP 数据')+
      stat('近 60 秒 WS 请求',logFresh && numeric(traffic.requests_60s)?traffic.requests_60s:'—','',logFresh?`P95 ${fixed(traffic.request_p95_ms,0)} ms · 错误 ${numeric(traffic.errors_60s)?traffic.errors_60s:'—'}`:'等待新鲜请求日志')+
      stat('观测入站流量',socketsFresh?rate(traffic.received_bps):'—','',socketsFresh?`出站 ${rate(traffic.sent_bps)}`:'等待新鲜 TCP 采样');
    $('#cluster-port-content').innerHTML=`<div class="cluster-port-heading"><strong>${escape(ports.host || '已配置入口')}</strong><span>探活周期 ${escape(numeric(interval)&&interval>0?interval:'未知')} 秒 · 最近探活 ${date(latest)} · 过期 ${stale} 项 · 北京时间</span></div><div class="cluster-summary">${summary}</div><div class="cluster-port-grid">${evaluated.map(({check,status})=>{
      const connections=socketsFresh?(traffic.by_port?.[String(check.port)] || 0):null;
      const title=`端口 ${check.port} · 检测 ${date(check.checked_at)} · ${status==='stale'?'探活结果已过期或时间待核对':status==='pending'?'尚无有效探活结果':'有效探活结果'}${check.error?` · ${check.error}`:''}`;
      return `<div class="cluster-port ${['up','down','pending'].includes(status)?status:'stale'}" title="${escape(title)}"><strong>${escape(check.port)}<i></i></strong><span>${numeric(connections)?connections+' 连接':'— 连接'}</span><small>${status==='up'?fixed(check.latency_ms,0)+' ms':status==='down'?'异常':status==='stale'?'旧读数':'待检测'}</small></div>`;
    }).join('')}</div><div class="cluster-source-heading"><h3>外部流量来源 <span>${sources.length}</span></h3><span>${!traffic?'等待采集':!trafficFresh?'旧快照':traffic.socket_error?'TCP 采集异常':'采集快照'}</span></div><div class="cluster-table-scroll" tabindex="0" role="region" aria-label="流量来源表，可横向滚动"><table class="cluster-sources"><thead><tr><th>来源</th><th>IP 地址</th><th>连接</th><th>端口</th><th>入站 ↓</th><th>出站 ↑</th><th>最近出现</th></tr></thead><tbody>${sources.map(source=>{
      const sourcePorts=Array.isArray(source.ports)?source.ports:[];
      return `<tr><td><span class="cluster-source-label ${source.known?'known':'unknown'}">${escape(source.label || '未知来源')}</span></td><td><button data-cluster-copy="${escape(source.ip)}" title="复制 IP">${escape(source.ip)}</button></td><td>${socketsFresh && numeric(source.connections)?source.connections:'—'}</td><td title="${escape(sourcePorts.join(', '))}">${escape(sourcePorts.slice(0,3).join(', ') || '—')}${sourcePorts.length>3?' +'+(sourcePorts.length-3):''}</td><td>${socketsFresh?rate(source.received_bps):'—'}</td><td>${socketsFresh?rate(source.sent_bps):'—'}</td><td>${since(source.last_seen)}</td></tr>`;
    }).join('')}</tbody></table></div>${!sources.length?'<p class="cluster-node-empty">当前没有观测到外部来源；无流量时列表保持为空。</p>':''}<p class="cluster-note">来源为 TCP 对端，NAT / 代理后显示出口 IP；未知来源不推断为官方。短连接可能漏采，HTTP 探活也可能计入连接，策略活动以 WS 请求数为准。</p><p class="cluster-note">流量采样 ${date(traffic?.sampled_at)} · 自 ${date(traffic?.observed_since)} 开始观测${traffic?.bridge?.pid?` · Bridge PID ${escape(traffic.bridge.pid)}`:''} · 最近请求 ${since(traffic?.last_request)}</p>`;
    applyMeters();
  }
  function render() {
    if(!current()) return;
    renderStatus();
    const snapshot=data?.snapshot;
    const messages={unconfigured:['尚未配置集群采集','可通过对话让 Agent 接入已有 SSH 机器并配置采集，配置完成后这里会显示资源快照。'],unavailable:['集群采集暂不可用','保留已有快照；可通过对话让 Agent 检查采集服务与连接。'],no_data:['等待首次采样','采集结果写入后，页面会自动显示机器和 GPU 状态。']};
    const message=messages[data?.state];
    const status=typeof data?.status==='string'?data.status:'';
    $('#cluster-notice').innerHTML=(message?`<div class="cluster-empty"><strong>${escape(status || message[0])}</strong><p>${status?'可通过对话让 Agent 根据此状态检查或更新采集服务；页面会继续读取已保存的状态。':message[1]}</p></div>`:status && data?.state!=='available'?`<p class="cluster-warning">${escape(status)}</p>`:'')+(data?.error?`<p class="cluster-warning">${escape(data.error)}</p>`:'')+(!fresh() && snapshot?`<p class="cluster-warning">${paused?'画面暂停，以下数值停留在当前采样。':!data.collector_running?'集群采集未运行，以下仅为保留的历史读数。':failed?'本次读取失败，以下保留上次快照。':'以下采样已过期，请结合采集时间判断。'}</p>`:'');
    $('#cluster-resources').hidden=!snapshot;
    $('#cluster-summary').hidden=!snapshot;
    if(!snapshot) {$('#cluster-ports').hidden=true;return;}
    const s=snapshot.summary || {};
    const windows=numeric(s.gpu_window_count)?s.gpu_window_count:0, singles=numeric(s.gpu_single_count)?s.gpu_single_count:0;
    const unknown=numeric(s.gpus)?Math.max(0,s.gpus-windows-singles):null;
    const sampling=singles>0?`含 ${singles} 张单次瞬时值 · ${windows} 张窗口采样`:windows>0 && unknown===0?'各卡近 60 秒均值的平均':unknown!==0?'含采样方式未声明的读数':'暂无 GPU 利用率采样';
    const active=windows>0?`窗口活跃 ${numeric(s.active_gpus)?s.active_gpus:'—'} / ${windows} 卡 · 峰值 ≥ 10%`:'暂无有效窗口采样，不判定窗口活跃';
    $('.cluster-caption > span:last-child').textContent=singles>0?'逐卡：窗口均值或单次瞬时值 / 显存':windows>0 && unknown===0?'逐卡：GPU 近 60 秒均值 / 显存':'逐卡：GPU 利用率 / 显存（采样方式见节点）';
    $('#cluster-resources > .cluster-note').textContent=[windows>0?'窗口采样显示近 60 秒均值与峰值。':'',singles>0?'单次回退显示瞬时值，窗口峰值未知，不计入窗口活跃。':'',unknown!==0?'未声明采样方式的读数不视为 60 秒窗口。':'','窗口活跃仅按明确窗口峰值 ≥ 10% 统计；过期节点不计入采集时的整体利用率。'].filter(Boolean).join('');
    $('#cluster-summary').innerHTML=stat(fresh()?'在线节点':'快照在线节点',numeric(s.online)?s.online:'—',numeric(s.nodes)?`/ ${s.nodes}`:'',`${numeric(s.gpus)?s.gpus:'—'} 张 GPU 纳入该次汇总`,numeric(s.online)&&numeric(s.nodes)&&s.nodes>0?100*s.online/s.nodes:null)+
      stat('GPU 平均利用率',percent(s.gpu_percent),'',`${sampling}<br>${active}`,s.gpu_percent)+
      stat('CPU 总体利用率',percent(s.cpu_percent),'','按该次在线机器的逻辑核心加权',s.cpu_percent)+
      stat('GPU 显存',gib(s.gpu_memory_used_mib),'GiB',`/ ${gib(s.gpu_memory_total_mib)} GiB · ${numeric(s.power_watts)?fixed(s.power_watts/1000)+' kW':'功耗未知'}`,numeric(s.gpu_memory_used_mib)&&numeric(s.gpu_memory_total_mib)&&s.gpu_memory_total_mib>0?100*s.gpu_memory_used_mib/s.gpu_memory_total_mib:null);
    const groups=[...new Set((snapshot.nodes || []).map(node=>node.group).filter(Boolean))].sort();
    if(filter && !groups.includes(filter)) filter='';
    $('#cluster-filters').innerHTML=[['','全部节点'],...groups.map(group=>[group,group])].map(([value,label])=>`<button data-cluster-filter="${escape(value)}" class="${filter===value?'selected':''}" aria-pressed="${filter===value}">${escape(label)}</button>`).join('');
    renderNodes();renderPorts();applyMeters();
  }
  async function refresh(manual=false) {
    if(!current() || pending || paused && !manual) return;
    const epoch=++request;
    pending=true;controller=new AbortController();renderStatus();
    const timeout=setTimeout(()=>controller?.abort(),10000);
    try {
      const next=await api('cluster',{}, {signal:controller.signal});
      if(!current() || epoch!==request) return;
      data=next;failed=false;render();onRead(next);
    } catch(error) {
      if(!current() || epoch!==request) return;
      failed=true;
      if(data) render();
      else {renderStatus();$('#cluster-notice').innerHTML=`<div class="cluster-empty"><strong>暂时无法读取集群快照</strong><p>${escape(error.name==='AbortError'?'读取超时，请稍后重试。':error.message)}</p></div>`;}
    } finally {
      clearTimeout(timeout);pending=false;controller=null;
      if(current()) renderStatus();
    }
  }
  function click(event) {
    const selected=event.target.closest('[data-cluster-filter]');
    if(selected) {filter=selected.dataset.clusterFilter;render();}
    if(event.target.closest('[data-cluster-pause]')) {
      paused=!paused;
      if(paused) {++request;controller?.abort();}
      render();
      if(!paused) refresh();
    }
    if(event.target.closest('[data-cluster-refresh]')) refresh(true);
    const copy=event.target.closest('[data-cluster-copy]');
    if(copy) navigator.clipboard?.writeText(copy.dataset.clusterCopy).then(()=>{if(current())copy.title='IP 已复制';}).catch(()=>{if(current())copy.title='可直接选中并复制 IP';});
  }
  function input(event) {
    if(event.target.id==='cluster-search') {search=event.target.value.trim().toLocaleLowerCase('zh-CN');renderNodes();}
  }
  target.addEventListener('click',click);target.addEventListener('input',input);target.addEventListener('change',change);
  const timer=setInterval(()=>refresh(),3000);
  refresh();
  return ()=>{closed=true;++request;clearInterval(timer);controller?.abort();target.removeEventListener('click',click);target.removeEventListener('input',input);target.removeEventListener('change',change);};
}
