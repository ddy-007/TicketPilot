import { useCallback, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { ApiError, errorMessage, request, submissionUncertain } from './api';
import type { Approval } from './api';
import { terminal } from './flow';
import { useAuth } from './hooks/useAuth';
import { usePolling } from './hooks/usePolling';
import { useNavigation } from './hooks/useNavigation';
import LoginForm from './components/LoginForm';
import TicketList from './components/TicketList';
import TicketDetail from './components/TicketDetail';
import RunView from './components/RunView';
import ApprovalCard from './components/ApprovalCard';
import './style.css';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function App() {
  // Ref-based callback so useNavigation can call polling.resetDomain without circular init.
  const resetRef = useRef<(id: string) => void>(() => {});
  const onReset = useCallback((id: string) => resetRef.current(id), []);

  const nav = useNavigation(onReset);
  const auth = useAuth(nav.viewVersion);
  const polling = usePolling(auth.session, nav.runId, nav.routeRevision, nav.viewVersion, auth.lock, auth.setBusy, auth.setError);

  // Wire the reset callback now that polling is available.
  resetRef.current = polling.resetDomain;

  const { session, input, setInput, error, setError, busy, setBusy, lock, login, logout: authLogout } = auth;
  const { runId, navigate } = nav;
  const {
    tickets, setTickets, nextAfter, selectedId, setSelectedId, ticket, setTicket,
    run, events, readError, setReadError, notice, setNotice,
    reason, setReason, uncertainDecision, setUncertainDecision,
    decisionSent, setDecisionSent, refresh, setRefresh,
    startKey, decisionAttempt, moreTickets, resetAll,
  } = polling;

  const logout = useCallback(() => {
    authLogout();
    resetAll();
  }, [authLogout, resetAll]);

  const start = useCallback(async () => {
    if (!session || !ticket || lock.current || session.who.role !== 'operator') return;
    lock.current = true; setBusy(true); setError('');
    const version = nav.viewVersion.current;
    if (startKey.current?.ticket !== ticket.id) startKey.current = { ticket: ticket.id, key: crypto.randomUUID() };
    const pendingUrl = new URL(location.href);
    pendingUrl.searchParams.set('ticket', ticket.id); pendingUrl.searchParams.set('request', startKey.current.key);
    history.replaceState({}, '', pendingUrl.pathname + pendingUrl.search);
    try {
      const result = await request<{ run_id: string }>(session.token, `/api/tickets/${encodeURIComponent(ticket.id)}/runs`, {
        method: 'POST', headers: { 'Idempotency-Key': startKey.current.key }, body: JSON.stringify({ auto_note: false }),
      });
      if (version !== nav.viewVersion.current) return;
      navigate(result.run_id); startKey.current = null;
    } catch (e) {
      if (version !== nav.viewVersion.current) return;
      if (e instanceof ApiError && e.code === 'ACTIVE_RUN_EXISTS' && e.runId && uuid.test(e.runId)) {
        navigate(e.runId); setNotice('此工单已有运行任务，已打开原任务。'); startKey.current = null;
      } else setError(errorMessage(e));
    } finally { lock.current = false; setBusy(false); }
  }, [session, ticket, lock, setBusy, setError, nav.viewVersion, startKey, navigate, setNotice]);

  const decide = useCallback(async (approval: Approval, decision: string) => {
    if (!session || lock.current || session.who.role !== 'approver') return;
    lock.current = true; setBusy(true); setError('');
    const version = nav.viewVersion.current;
    const previous = decisionAttempt.current;
    const attempt = previous?.id === approval.approval_id ? previous : {
      id: approval.approval_id, decision, hash: approval.plan_hash, reason,
    };
    decisionAttempt.current = attempt;
    try {
      await request(session.token, `/api/approvals/${attempt.id}/decision`, {
        method: 'POST', body: JSON.stringify({ decision: attempt.decision, plan_hash: attempt.hash, reason: attempt.reason }),
      });
      if (version !== nav.viewVersion.current) return;
      setNotice('审批决定已提交，等待服务端处理；尚未确认执行结果。');
      setDecisionSent(true);
      setUncertainDecision(null); decisionAttempt.current = null; setRefresh(n => n + 1);
    } catch (e) {
      if (version !== nav.viewVersion.current) return;
      setError(errorMessage(e));
      if (submissionUncertain(e)) {
        decisionAttempt.current = attempt;
        setUncertainDecision(attempt.decision);
      } else { setNotice(''); setUncertainDecision(null); decisionAttempt.current = null; setRefresh(n => n + 1); }
    } finally { lock.current = false; setBusy(false); }
  }, [session, lock, setBusy, setError, nav.viewVersion, reason, decisionAttempt, setNotice, setDecisionSent, setUncertainDecision, setRefresh]);

  const handleSelectTicket = useCallback((id: string) => {
    setTicket(null); setSelectedId(id); startKey.current = null; setError(''); setReadError('');
  }, [setTicket, setSelectedId, startKey, setError, setReadError]);

  const handleGoList = useCallback(() => {
    navigate(''); setTicket(null); startKey.current = null;
  }, [navigate, setTicket, startKey]);

  const handleRefresh = useCallback(() => setRefresh(n => n + 1), [setRefresh]);

  const handleHeaderHome = useCallback((e: React.MouseEvent) => {
    e.preventDefault(); navigate(''); setTicket(null);
  }, [navigate, setTicket]);

  const handleViewExistingRun = useCallback(() => {
    if (ticket?.active_run_id) navigate(ticket.active_run_id);
  }, [navigate, ticket]);

  const isOperator = session?.who.role === 'operator';
  const isApprover = session?.who.role === 'approver';

  return <>
    <header><a href="/" onClick={handleHeaderHome}>TicketPilot</a><span>售后工单工作台</span>
      {session && <div className="identity"><span>{isOperator ? '客服操作员' : '审批员'} · {session.who.tenant}</span><button onClick={logout} disabled={busy}>切换身份</button></div>}
    </header>
    <main>
      {!session ? <LoginForm runId={runId} input={input} setInput={setInput} busy={busy} error={error} readError={readError} login={login} />
      : <>
        <nav><button onClick={handleGoList} disabled={busy}>工单列表</button>
          <button onClick={handleRefresh} disabled={busy}>刷新状态</button><span>单 Agent · 人工审批 · 持久恢复</span></nav>
        {(error || readError) && <p className="error" role="alert">{[error, readError].filter(Boolean).join(' ')}</p>}
        {notice && <p className="notice" role="status">{notice}</p>}
        {!runId && !selectedId && !ticket && <TicketList tickets={tickets} nextAfter={nextAfter} busy={busy} error={error} readError={readError} onSelect={handleSelectTicket} onMore={() => void moreTickets()} />}
        {(ticket || runId || selectedId) && <div className="workspace">
          <TicketDetail ticket={ticket} runId={runId} busy={busy} readError={readError} isOperator={!!isOperator} onStart={start} onViewExistingRun={handleViewExistingRun} />
          {runId && <RunView run={run} events={events} readError={readError} />}
          {runId && <ApprovalCard run={run} isApprover={!!isApprover} readError={readError} busy={busy}
            reason={reason} setReason={setReason} uncertainDecision={uncertainDecision} decisionSent={decisionSent} onDecide={decide} />}
        </div>}
      </>}
    </main><footer>仅用于本机合成数据演示 · 履约重试仅表示重新入队 · 工单保持打开</footer>
  </>;
}

createRoot(document.getElementById('root')!).render(<App />);
