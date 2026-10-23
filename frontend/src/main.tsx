import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, errorMessage, request, submissionUncertain } from './api';
import type { Approval, Identity, Run, RunEvent, Ticket, TicketPage, TicketSummary } from './api';
import { drainEvents, mergeEvents, resultTitle, statusLabels, terminal } from './flow.js';
import './style.css';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function locationRun() { const value = new URL(location.href).searchParams.get('run'); return value && uuid.test(value) ? value : ''; }
function date(value: string) { return new Date(value).toLocaleString('zh-CN'); }
function Json({ value }: { value: unknown }) { return <pre>{JSON.stringify(value, null, 2)}</pre>; }

function App() {
  const [session, setSession] = useState<{ token: string; who: Identity } | null>(null);
  const [input, setInput] = useState('');
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [runId, setRunId] = useState(locationRun);
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [refresh, setRefresh] = useState(0);
  const [notice, setNotice] = useState('');
  const lock = useRef(false);
  const startKey = useRef<{ ticket: string; key: string } | null>(null);
  const decisionAttempt = useRef<{ id: string; decision: string; hash: string; reason: string } | null>(null);
  const [reason, setReason] = useState('');
  const [uncertainDecision, setUncertainDecision] = useState<string | null>(null);
  const [decisionSent, setDecisionSent] = useState(false);
  const viewVersion = useRef(0);
  const [routeRevision, setRouteRevision] = useState(0);

  function resetView(id: string) {
    viewVersion.current++;
    setRouteRevision(n => n + 1);
    setRunId(id); setRun(null); setTicket(null); setSelectedId(''); setEvents([]); setError(''); setReadError(''); setNotice('');
    setReason(''); setUncertainDecision(null); setDecisionSent(false); decisionAttempt.current = null;
  }

  function navigate(id: string) {
    const url = new URL(location.href);
    url.searchParams.delete('ticket'); url.searchParams.delete('request');
    id ? url.searchParams.set('run', id) : url.searchParams.delete('run');
    history.pushState({}, '', url.pathname + url.search);
    resetView(id);
  }

  useEffect(() => {
    const changed = () => resetView(locationRun());
    window.addEventListener('popstate', changed);
    return () => window.removeEventListener('popstate', changed);
  }, []);

  useEffect(() => {
    if (!session) return;
    let active = true;
    const version = viewVersion.current;
    const isCurrent = () => active && version === viewVersion.current;
    const controller = new AbortController();
    setReadError('');
    if (!runId) {
      request<TicketPage>(session.token, '/api/tickets', { signal: controller.signal })
        .then(data => {
          if (!isCurrent()) return;
          setTickets(data.tickets);
          setNextAfter(data.next_after_id);
          const query = new URL(location.href).searchParams;
          const key = query.get('request');
          const pending = query.get('ticket');
          if (pending && /^[A-Za-z0-9_-]{1,64}$/.test(pending) && key && uuid.test(key)) {
            startKey.current = { ticket: pending, key };
            setSelectedId(pending);
          }
        })
        .catch(e => { if (isCurrent()) setReadError(errorMessage(e)); });
    } else {
      let timer: ReturnType<typeof setTimeout>;
      let cursor = 0;
      const poll = async () => {
        try {
          const current = await request<Run>(session.token, `/api/runs/${runId}`, { signal: controller.signal });
          if (!isCurrent()) return;
          setRun(current);
          if (terminal.has(current.status)) { setNotice(''); setUncertainDecision(null); setError(''); }
          const attempt = decisionAttempt.current;
          if (attempt && attempt.id === current.approval?.approval_id && current.approval?.decision !== 'PENDING') {
            setUncertainDecision(null); setError(''); decisionAttempt.current = null;
          }
          const issues: string[] = [];
          try {
            const detail = await request<Ticket>(session.token, `/api/tickets/${encodeURIComponent(current.ticket_id)}`, { signal: controller.signal });
            if (!isCurrent()) return;
            setTicket(detail);
          } catch (e) { if (isCurrent()) issues.push(errorMessage(e)); }
          // Audit retrieval is independent of the optional business ticket lookup.
          try {
            cursor = await drainEvents(
              (after: number) => request<{ events: RunEvent[] }>(session.token, `/api/runs/${runId}/events?after_id=${after}&limit=100`, { signal: controller.signal }),
              cursor, (next: RunEvent[]) => setEvents(previous => mergeEvents(previous, next)), isCurrent,
            );
          } catch (e) { if (isCurrent()) issues.push(errorMessage(e)); }
          if (!isCurrent()) return;
          setReadError(issues.join(' '));
          if (!issues.length && !terminal.has(current.status)) timer = setTimeout(poll, current.status === 'WAITING_APPROVAL' ? 3000 : 1500);
        } catch (e) {
          if (isCurrent()) setReadError(errorMessage(e));
        }
      };
      void poll();
      return () => { active = false; controller.abort(); clearTimeout(timer); };
    }
    return () => { active = false; controller.abort(); };
  }, [session, runId, refresh, routeRevision]);

  useEffect(() => {
    if (!session || runId || !selectedId) return;
    let active = true;
    const version = viewVersion.current;
    const controller = new AbortController();
    request<Ticket>(session.token, `/api/tickets/${encodeURIComponent(selectedId)}`, { signal: controller.signal })
      .then(detail => { if (active && version === viewVersion.current) { setTicket(detail); setReadError(''); } })
      .catch(e => { if (active && version === viewVersion.current) setReadError(errorMessage(e)); });
    return () => { active = false; controller.abort(); };
  }, [session, runId, selectedId, refresh, routeRevision]);

  async function moreTickets() {
    if (!session || !nextAfter || lock.current) return;
    lock.current = true; setBusy(true); setReadError('');
    const version = viewVersion.current;
    try {
      const data = await request<TicketPage>(session.token, `/api/tickets?after_id=${encodeURIComponent(nextAfter)}`);
      if (version !== viewVersion.current) return;
      setTickets(previous => [...new Map([...previous, ...data.tickets].map(t => [t.id, t])).values()]);
      setNextAfter(data.next_after_id);
    } catch (e) { if (version === viewVersion.current) setReadError(errorMessage(e)); }
    finally { lock.current = false; setBusy(false); }
  }

  async function login(event: React.FormEvent) {
    event.preventDefault();
    if (lock.current) return;
    lock.current = true; setBusy(true); setError('');
    const version = viewVersion.current;
    const token = input; setInput('');
    try {
      const who = await request<Identity>(token, '/api/me');
      if (version !== viewVersion.current) return;
      setSession({ token, who }); setTicket(null); setTickets([]);
    } catch (e) { if (version === viewVersion.current) setError(errorMessage(e)); }
    finally { lock.current = false; setBusy(false); }
  }

  function logout() {
    viewVersion.current++;
    setSession(null); setInput(''); setTicket(null); setSelectedId(''); setTickets([]); setRun(null); setEvents([]);
    setError(''); setReadError(''); setNotice(''); setReason(''); setUncertainDecision(null); setDecisionSent(false); decisionAttempt.current = null;
  }

  async function start() {
    if (!session || !ticket || lock.current || session.who.role !== 'operator') return;
    lock.current = true; setBusy(true); setError('');
    const version = viewVersion.current;
    if (startKey.current?.ticket !== ticket.id) startKey.current = { ticket: ticket.id, key: crypto.randomUUID() };
    const pendingUrl = new URL(location.href);
    pendingUrl.searchParams.set('ticket', ticket.id); pendingUrl.searchParams.set('request', startKey.current.key);
    history.replaceState({}, '', pendingUrl.pathname + pendingUrl.search);
    try {
      const result = await request<{ run_id: string }>(session.token, `/api/tickets/${encodeURIComponent(ticket.id)}/runs`, {
        method: 'POST', headers: { 'Idempotency-Key': startKey.current.key }, body: JSON.stringify({ auto_note: false }),
      });
      if (version !== viewVersion.current) return;
      navigate(result.run_id); startKey.current = null;
    } catch (e) {
      if (version !== viewVersion.current) return;
      if (e instanceof ApiError && e.code === 'ACTIVE_RUN_EXISTS' && e.runId && uuid.test(e.runId)) {
        navigate(e.runId); setNotice('此工单已有运行任务，已打开原任务。'); startKey.current = null;
      } else setError(errorMessage(e));
    } finally { lock.current = false; setBusy(false); }
  }

  async function decide(approval: Approval, decision: string) {
    if (!session || lock.current || session.who.role !== 'approver') return;
    lock.current = true; setBusy(true); setError('');
    const version = viewVersion.current;
    const previous = decisionAttempt.current;
    const attempt = previous?.id === approval.approval_id ? previous : {
      id: approval.approval_id, decision, hash: approval.plan_hash, reason,
    };
    decisionAttempt.current = attempt;
    try {
      await request(session.token, `/api/approvals/${attempt.id}/decision`, {
        method: 'POST', body: JSON.stringify({ decision: attempt.decision, plan_hash: attempt.hash, reason: attempt.reason }),
      });
      if (version !== viewVersion.current) return;
      setNotice('审批决定已提交，等待服务端处理；尚未确认执行结果。');
      setDecisionSent(true);
      setUncertainDecision(null); decisionAttempt.current = null; setRefresh(n => n + 1);
    } catch (e) {
      if (version !== viewVersion.current) return;
      setError(errorMessage(e));
      if (submissionUncertain(e)) {
        decisionAttempt.current = attempt;
        setUncertainDecision(attempt.decision);
      } else { setNotice(''); setUncertainDecision(null); decisionAttempt.current = null; setRefresh(n => n + 1); }
    } finally { lock.current = false; setBusy(false); }
  }

  const approval = run?.approval;
  const expired = approval ? Date.now() >= Date.parse(approval.expires_at) : false;
  const mayDecide = !!session && session.who.role === 'approver' && run?.status === 'WAITING_APPROVAL' && approval?.decision === 'PENDING' && !expired && !readError;
  return <>
    <header><a href="/" onClick={e => { e.preventDefault(); navigate(''); setTicket(null); }}>TicketPilot</a><span>售后工单工作台</span>
      {session && <div className="identity"><span>{session.who.role === 'operator' ? '客服操作员' : '审批员'} · {session.who.tenant}</span><button onClick={logout} disabled={busy}>切换身份</button></div>}
    </header>
    <main>
      {(error || readError) && <p className="error" role="alert">{[error, readError].filter(Boolean).join(' ')}</p>}
      {!session ? <section className="login"><p className="eyebrow">本机合成数据演示</p><h1>连接工作台</h1>
        <p>输入操作员或审批员 Token。身份由服务端确认，Token 只保留在当前页面内存，刷新后需要重新输入。</p>
        {runId && <p>登录后恢复任务 <code>{runId}</code>，不会创建新任务。</p>}
        <form onSubmit={login}><label htmlFor="token">角色 Token</label><input id="token" type="password" autoComplete="off" spellCheck={false} value={input} onChange={e => setInput(e.target.value)} maxLength={256} required />
          <button className="primary" disabled={busy}>{busy ? '验证中…' : '连接'}</button></form>
      </section> : <>
        <nav><button onClick={() => { navigate(''); setTicket(null); startKey.current = null; }} disabled={busy}>工单列表</button>
          <button onClick={() => setRefresh(n => n + 1)} disabled={busy}>刷新状态</button><span>单 Agent · 人工审批 · 持久恢复</span></nav>
        {notice && <p className="notice" role="status">{notice}</p>}
        {!runId && !selectedId && !ticket && <section><h1>待处理工单</h1><p className="muted">选择工单，查看绑定订单后发起诊断。</p>
          <div className="ticket-list">{tickets.map(t => <button className="ticket" key={t.id} onClick={() => { setTicket(null); setSelectedId(t.id); startKey.current = null; setError(''); setReadError(''); }}>
            <strong>{t.id}</strong><span>{t.subject}</span><small>绑定订单 {t.bound_order_id || '未绑定'} · {t.status === 'OPEN' ? '打开' : '关闭'}{t.active_run_id ? ' · 存在运行任务' : ''}</small>
          </button>)}</div>{nextAfter && <button onClick={() => void moreTickets()} disabled={busy}>加载更多工单</button>}{!tickets.length && !error && !readError && <p>暂无工单或正在加载…</p>}</section>}
        {(ticket || runId || selectedId) && <div className="workspace">
          <section><p className="eyebrow">工单详情</p><h1>{ticket?.id || '加载工单…'}</h1><h2>{ticket?.subject}</h2><p className="ticket-body">{ticket?.body}</p>
            <dl><dt>绑定订单</dt><dd>{ticket?.bound_order_id || '未绑定'}</dd><dt>工单状态</dt><dd>{ticket?.status === 'OPEN' ? '打开' : ticket?.status}</dd></dl>
            {!runId && <><button className="primary" onClick={start} disabled={busy || session.who.role !== 'operator' || !ticket || !!readError}>{busy ? '提交中…' : '开始诊断'}</button>
              {ticket?.active_run_id && <button onClick={() => navigate(ticket.active_run_id!)} disabled={busy}>查看现有任务</button>}
              {session.who.role !== 'operator' && <p className="muted">仅客服操作员可以发起诊断。</p>}</>}
            {runId && <><p className="muted">任务 ID</p><code className="break">{runId}</code><p className="muted">刷新或复制当前网址可恢复此任务。</p></>}
          </section>
          {runId && <section><p className="eyebrow">诊断与执行</p><h2>{run ? resultTitle(run) : '加载运行状态…'}</h2>
            {readError && <p className="muted">部分信息读取失败，当前内容可能不是最新状态。请刷新核对。</p>}
            <p className="badge">{run ? statusLabels[run.status as keyof typeof statusLabels] || run.status : '读取中'}</p>
            {run?.result_json && <div className="result"><p>{run.result_json.summary}</p><code>{run.result_json.reason_code}</code>{run.result_json.warnings?.map(w => <p key={w}>{w}</p>)}</div>}
            <h3>调用轨迹 <small>{events.length} 条</small></h3><ol className="timeline">{events.map(e => <li key={e.id}><strong>{e.event_type}</strong><small>{date(e.created_at)} · #{e.id}</small>
              <details><summary>事件详情</summary><Json value={e.payload_json} /></details></li>)}</ol>
            <h3>证据 <small>{run?.evidence.length || 0} 条</small></h3>{run?.evidence.map(e => <details key={e.evidence_id}><summary>{e.tool_name} · {e.ok ? '成功' : '失败'} · {e.evidence_id}</summary><Json value={e} /></details>)}
          </section>}
          {runId && <section><p className="eyebrow">人工控制</p><h2>审批卡片</h2>{approval ? <>
            <p className="badge">{approval.decision === 'PENDING' ? (expired ? '审批已过期' : '等待决定') : { APPROVED: '已批准', REJECTED: '已拒绝', EXPIRED: '已过期' }[approval.decision] || approval.decision}</p>
            <dl><dt>操作</dt><dd>{approval.plan.tool}</dd><dt>订单</dt><dd>{approval.plan.args.order_id}</dd><dt>预期版本</dt><dd>{approval.plan.args.expected_version}</dd>
              <dt>失败码</dt><dd>{approval.plan.args.expected_error_code}</dd><dt>有效期</dt><dd>{date(approval.expires_at)}</dd></dl>
            <p>{approval.plan.reason}</p><p className="notice">{approval.plan.expected_effect}</p>
            <p>证据：{approval.plan.evidence_ids.join('、')}</p><details><summary>完整计划与服务端哈希</summary><Json value={approval.plan} /><code className="break">{approval.plan_hash}</code></details>
            {approval.reason && <p>审批理由：{approval.reason}</p>}
            {session.who.role !== 'approver' && <p className="muted">当前为客服操作员，无审批权限。请切换审批员身份。</p>}
            {approval.decision === 'PENDING' && <><label htmlFor="reason">审批理由（可选，请勿填写凭证或个人信息）</label><textarea id="reason" value={reason} onChange={e => setReason(e.target.value)} maxLength={500} disabled={!mayDecide || busy || !!uncertainDecision} />
              <div className="actions"><button className="primary" onClick={() => void decide(approval, 'APPROVED')} disabled={!mayDecide || busy || !!uncertainDecision || decisionSent}>批准履约重试</button>
                <button onClick={() => void decide(approval, 'REJECTED')} disabled={!mayDecide || busy || !!uncertainDecision || decisionSent}>拒绝</button></div>
              {uncertainDecision && <button onClick={() => void decide(approval, uncertainDecision)} disabled={busy}>重试原审批决定</button>}</>}
          </> : <p className="muted">{run && terminal.has(run.status) ? '本轮没有待审批提案。' : '完成诊断后，符合规则的提案将在此展示。'}</p>}</section>}
        </div>}
      </>}
    </main><footer>仅用于本机合成数据演示 · 履约重试仅表示重新入队 · 工单保持打开</footer>
  </>;
}

createRoot(document.getElementById('root')!).render(<App />);
