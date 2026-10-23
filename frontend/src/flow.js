export const terminal = new Set(['SUCCEEDED', 'REJECTED', 'EXPIRED', 'ESCALATED', 'FAILED']);

export const statusLabels = {
  QUEUED: '任务排队中', RUNNING: '正在诊断或执行', WAITING_APPROVAL: '等待人工审批',
  SUCCEEDED: '本轮已完成', REJECTED: '审批已拒绝', EXPIRED: '审批已过期',
  ESCALATED: '需要人工处理', FAILED: '本轮失败',
};

export function resultTitle(run) {
  if (run.status === 'SUCCEEDED' && run.disposition === 'REQUEUED' &&
      run.result_json?.operation?.status === 'CONFIRMED') return '已重新入队';
  const code = run.result_json?.reason_code;
  if (code === 'APPROVAL_EXPIRED') return '审批已过期';
  if (code === 'STALE_PLAN') return '提案已失效，需重新诊断';
  if (code === 'UNKNOWN_OPERATION') return '结果未知，需人工核对';
  if (code === 'VERIFICATION_FAILED') return '提交后验证未通过';
  if (run.disposition === 'NO_ACTION_REQUIRED') return '无需执行操作';
  return statusLabels[run.status] || '未知状态';
}

export function mergeEvents(current, next) {
  return [...new Map([...current, ...next].map(e => [e.id, e])).values()]
    .sort((a, b) => a.id - b.id);
}

export async function drainEvents(fetchPage, cursor, receive, alive = () => true) {
  while (alive()) {
    const page = await fetchPage(cursor);
    if (!alive()) return cursor;
    const next = page.events;
    if (!next.length) return cursor;
    const last = Math.max(...next.map(e => e.id));
    if (last <= cursor) throw new Error('EVENT_CURSOR_STALLED');
    receive(next);
    cursor = last;
    if (next.length < 100) return cursor;
  }
  return cursor;
}
