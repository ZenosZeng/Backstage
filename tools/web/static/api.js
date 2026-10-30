// The published demo replaces this module with its static-data implementation.
// The local workbench always reads its existing, read-only HTTP endpoints.
export async function api(path, params = {}, {signal} = {}) {
  const response = await fetch('api/' + path + '?' + new URLSearchParams(params), {
    cache: 'no-store', signal,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || '读取失败');
  return data;
}
