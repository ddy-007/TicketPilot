export type Identity = { user: string; role: 'operator' | 'approver'; tenant: string };
export type Ticket = { id: string; subject: string; body: string; bound_order_id: string | null; status: string; active_run_id?: string | null };
export type TicketSummary = Omit<Ticket, 'body'>;
export type TicketPage = { tickets: TicketSummary[]; next_after_id: string | null };
export type Evidence = { evidence_id: string; tool_name: string; ok: boolean; data: unknown; error: unknown; meta: unknown; attempts: unknown[] };
export type RunEvent = { id: number; event_type: string; payload_json: Record<string, unknown>; created_at: string };
export type Approval = {
  approval_id: string; decision: string; plan_hash: string; expires_at: string; reason: string | null;
  plan: { tool: string; args: { order_id: string; expected_version: number; expected_error_code: string };
    evidence_ids: string[]; reason: string; expected_effect: string; expires_at: string };
};
export type Run = {
  run_id: string; ticket_id: string; status: string; disposition: string | null;
  approval?: Approval; evidence: Evidence[];
  result_json?: { summary: string; reason_code: string; warnings?: string[]; operation?: { status: string } };
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, public runId?: string) { super(code); }
}

export function submissionUncertain(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 0 || error.status === 408 || error.status >= 500);
}

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const identifier = /^[A-Za-z0-9_-]{1,64}$/;
const states = new Set(['QUEUED', 'RUNNING', 'WAITING_APPROVAL', 'SUCCEEDED', 'REJECTED', 'EXPIRED', 'ESCALATED', 'FAILED']);
const record = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === 'string';
const id = (v: unknown): v is string => text(v) && identifier.test(v);
const runId = (v: unknown): v is string => text(v) && uuid.test(v);
const timestamp = (v: unknown): v is string => text(v) && Number.isFinite(Date.parse(v));

function ticketReply(v: unknown, detail = true): boolean {
  return record(v) && id(v.id) && text(v.subject) && (!detail || text(v.body)) &&
    (v.bound_order_id === null || id(v.bound_order_id)) && ['OPEN', 'CLOSED'].includes(String(v.status)) &&
    (v.active_run_id === undefined || v.active_run_id === null || runId(v.active_run_id));
}

function approvalReply(v: unknown): boolean {
  if (!record(v) || !runId(v.approval_id) || !text(v.plan_hash) || !/^[a-f0-9]{64}$/.test(v.plan_hash) ||
      !timestamp(v.expires_at) || !['PENDING', 'APPROVED', 'REJECTED', 'EXPIRED'].includes(String(v.decision))) return false;
  const p = v.plan;
  return record(p) && p.tool === 'retry_fulfillment' && record(p.args) && id(p.args.order_id) &&
    Number.isSafeInteger(p.args.expected_version) && Number(p.args.expected_version) > 0 &&
    p.args.expected_error_code === 'QUEUE_PUBLISH_TIMEOUT' && text(p.reason) && text(p.expected_effect) &&
    timestamp(p.expires_at) && Array.isArray(p.evidence_ids) && p.evidence_ids.every(text) && (v.reason === null || text(v.reason));
}

// Validate the fields the UI consumes; TypeScript casts alone do not validate HTTP data.
function validReply(path: string, value: unknown, init: RequestInit): boolean {
  const route = path.split('?')[0];
  if (!record(value)) return false;
  if (route === '/api/me') return text(value.user) && text(value.tenant) &&
    (value.role === 'operator' || value.role === 'approver');
  if (route === '/api/tickets') {
    const after = new URL(path, location.origin).searchParams.get('after_id') || '';
    return Array.isArray(value.tickets) && value.tickets.length <= 25 &&
      value.tickets.every(t => ticketReply(t, false)) &&
      (value.next_after_id === null || (id(value.next_after_id) && value.next_after_id !== after &&
        value.next_after_id === value.tickets.at(-1)?.id));
  }
  if (/^\/api\/tickets\/[^/]+$/.test(route)) return ticketReply(value) && value.id === decodeURIComponent(route.split('/')[3]);
  if (/^\/api\/tickets\/[^/]+\/runs$/.test(route)) return runId(value.run_id) && states.has(String(value.status));
  if (/^\/api\/runs\/[^/]+\/events$/.test(route)) return Array.isArray(value.events) && value.events.length <= 100 && value.events.every(
    e => record(e) && Number.isSafeInteger(e.id) && Number(e.id) > 0 && text(e.event_type) && timestamp(e.created_at) && record(e.payload_json),
  );
  if (/^\/api\/runs\/[^/]+$/.test(route)) {
    if (!runId(value.run_id) || value.run_id.toLowerCase() !== route.split('/')[3].toLowerCase() ||
        !id(value.ticket_id) || !states.has(String(value.status)) ||
        !Array.isArray(value.evidence) || !value.evidence.every(e => record(e) && text(e.evidence_id) && text(e.tool_name) && typeof e.ok === 'boolean')) return false;
    if (value.approval !== undefined && value.approval !== null && !approvalReply(value.approval)) return false;
    const result = value.result_json;
    return result === undefined || result === null || (record(result) && text(result.summary) && text(result.reason_code) &&
      (result.warnings === undefined || (Array.isArray(result.warnings) && result.warnings.every(text))) &&
      (result.operation === undefined || result.operation === null || (record(result.operation) && text(result.operation.status))));
  }
  if (/^\/api\/approvals\/[^/]+\/decision$/.test(route)) {
    const sent = JSON.parse(String(init.body));
    return runId(value.approval_id) && route.split('/')[3] === value.approval_id && value.decision === sent.decision;
  }
  return false;
}

const messages: Record<string, string> = {
  UNAUTHORIZED: '身份凭证无效，请重新输入 Token。', FORBIDDEN: '当前角色没有此操作权限。',
  DEMO_AUTH_NOT_CONFIGURED: '服务端角色 Token 配置不合规，请联系本地开发者。',
  BUSINESS_UNAVAILABLE: '业务服务暂不可用，请稍后刷新。', TICKET_UNAVAILABLE: '工单暂不可用。',
  DATABASE_UNAVAILABLE: '数据库暂不可用，请稍后刷新。', NOT_FOUND: '任务不存在或当前身份无权访问。',
  PLAN_HASH_MISMATCH: '审批计划已变化，请刷新后核对。', DECISION_CONFLICT: '此审批已有其他决定，请刷新。',
  RUN_NOT_WAITING: '任务当前不在等待审批状态，请刷新。', EXPIRED: '审批已过期，需重新诊断。',
  INVALID_REQUEST: '请求无效，请刷新后重试。', NETWORK_ERROR: '连接失败，操作结果尚未确认。请刷新查看，重试会使用原请求。',
  EVENT_CURSOR_STALLED: '轨迹更新异常，请刷新重试。', IDEMPOTENCY_CONFLICT: '诊断请求冲突，请返回列表后重试。',
  INVALID_RESPONSE: '服务响应无效，请刷新核对；未确认的提交可重试原请求。',
};

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? messages[error.code] || `请求未完成（HTTP ${error.status}），请刷新核对。`
    : '连接或数据异常，请刷新重试。';
}

export async function request<T>(token: string, path: string, init: RequestInit = {}): Promise<T> {
  // Only same-origin API paths receive the role credential. No redirects or browser persistence.
  if (!path.startsWith('/api/')) throw new Error('INVALID_API_PATH');
  let response: Response;
  try {
    response = await fetch(path, {
      ...init, redirect: 'error', credentials: 'omit', cache: 'no-store',
      signal: AbortSignal.any([AbortSignal.timeout(15000), ...(init.signal ? [init.signal] : [])]),
      headers: { ...init.headers, Authorization: `Bearer ${token}`,
        ...(init.body ? { 'Content-Type': 'application/json' } : {}) },
    });
  } catch { throw new ApiError(0, 'NETWORK_ERROR'); }
  let body: unknown;
  try { body = await response.json(); }
  catch { throw new ApiError(response.ok ? 0 : response.status, 'INVALID_RESPONSE'); }
  if (!response.ok) {
    const detail = record(body) ? body.detail ?? body.error : null;
    throw new ApiError(response.status, text(detail) ? detail : record(detail) && text(detail.code) ? detail.code : 'UNKNOWN',
      record(detail) && runId(detail.run_id) ? detail.run_id : undefined);
  }
  if (!validReply(path, body, init)) throw new ApiError(0, 'INVALID_RESPONSE');
  return body as T;
}
