// This is the demo's complete transport. There is no live-API fallback.
// All records are synthetic; upstream project names illustrate possible use.
let dataset;
const copy = value => structuredClone(value);
const fold = value => String(value || '').normalize('NFKC').toLocaleLowerCase('zh-CN');
const notFound = () => { throw new Error('合成示例中没有这项内容，请返回项目列表。'); };

async function load() {
  if (!dataset) {
    dataset = fetch(new URL('data.json', import.meta.url)).then(async response => {
      if (!response.ok) throw new Error('无法读取合成 Demo 数据，请刷新页面。');
      return response.json();
    }).catch(error => { dataset = null; throw error; });
  }
  return dataset;
}

function reviewPacket(original, params) {
  const packet = copy(original);
  const limit = params.limit == null ? 20 : Number(params.limit);
  const max = params.max_chars == null ? 12000 : Number(params.max_chars);
  if (!Number.isInteger(limit) || limit < 1 || limit > 50 ||
      !Number.isInteger(max) || max < 1000 || max > 30000) {
    throw new Error('审查条数须为 1–50，字符预算须为 1000–30000');
  }
  packet.candidates = packet.candidates.slice(0, limit);
  packet.budget.limit = limit;
  packet.budget.max_chars = max;
  const measure = () => {
    packet.omitted = packet.current_total - packet.candidates.length;
    packet.budget.used_chars = 0;
    let size;
    do {
      size = packet.budget.used_chars;
      packet.budget.used_chars = [...JSON.stringify(packet)].length;
    } while (size !== packet.budget.used_chars);
    return packet.budget.used_chars;
  };
  while (measure() > max && packet.candidates.length) packet.candidates.pop();
  if (measure() > max) throw new Error('字符预算不足以容纳示例审查说明。');
  return packet;
}

export async function api(path, params = {}, {signal} = {}) {
  signal?.throwIfAborted();
  const data = await load();
  signal?.throwIfAborted();
  let value;
  switch (path) {
    case 'overview': value = data.overview; break;
    case 'timeline':
      value = params.task ? data.task_timelines[params.task]
        : params.project ? data.project_timelines[params.project] : data.timeline;
      if (params.project && value?.projects[0]?.name !== params.project) return notFound();
      break;
    case 'documents': {
      const project = params.business ? data.overview.projects.find(p => p.name === params.business) : null;
      if (params.business && !project) return notFound();
      const paths = project ? new Set(project.documents.map(item => typeof item === 'string' ? item : item.path)) : null;
      const terms = fold(params.q).trim().split(/\s+/).filter(Boolean);
      value = {items: data.documents.filter(doc => (!paths || paths.has(doc.path)) &&
        terms.every(term => fold([doc.title, doc.path, doc.excerpt, doc.search_text].join(' ')).includes(term)))};
      break;
    }
    case 'document': value = data.document_previews[params.path]; break;
    case 'event': value = data.events[params.id]; break;
    case 'handoff':
      value = copy(data.handoffs[params.task] || null);
      if (value) {
        const target = params.to_agent || null;
        if (target && !['codex', 'claude', 'kimi', 'dsh'].includes(target)) throw new Error('不支持的交接目标');
        value.handoff = {state: 'prepared', to_agent: target, dispatched: false};
      }
      break;
    case 'review':
      if (!data.reviews[params.task]) return notFound();
      value = reviewPacket(data.reviews[params.task], params);
      break;
    case 'services': value = data.services; break;
    case 'doctor': value = data.doctor; break;
    case 'monitors': value = data.monitors; break;
    case 'monitor': value = data.monitor_details[params.kind]; break;
    case 'cluster': value = data.cluster; break;
    default: return notFound();
  }
  if (value == null) return notFound();
  return copy(value);
}
