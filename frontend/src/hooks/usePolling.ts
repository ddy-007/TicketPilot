import { useEffect, useRef, useState } from 'react';
import { errorMessage, request } from '../api';
import type { Run, RunEvent, Ticket, TicketPage, TicketSummary } from '../api';
import { drainEvents, mergeEvents, terminal } from '../flow';
import type { Session } from './useAuth';

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Manages data fetching, polling, and all domain state for tickets, runs, events, approvals.
 */
export function usePolling(
  session: Session | null,
  runId: string,
  routeRevision: number,
  viewVersion: React.RefObject<number>,
  lock: React.RefObject<boolean>,
  setBusy: React.Dispatch<React.SetStateAction<boolean>>,
  setError: React.Dispatch<React.SetStateAction<string>>,
) {
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [readError, setReadError] = useState('');
  const [notice, setNotice] = useState('');
  const [reason, setReason] = useState('');
  const [uncertainDecision, setUncertainDecision] = useState<string | null>(null);
  const [decisionSent, setDecisionSent] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const startKey = useRef<{ ticket: string; key: string } | null>(null);
  const decisionAttempt = useRef<{ id: string; decision: string; hash: string; reason: string } | null>(null);

  /** Reset all domain state (called by navigation reset). */
  function resetDomain() {
    setRun(null); setTicket(null); setSelectedId(''); setEvents([]);
    setError(''); setReadError(''); setNotice(''); setReason('');
    setUncertainDecision(null); setDecisionSent(false); decisionAttempt.current = null;
  }

  /** Reset state on logout. */
  function resetAll() {
    resetDomain();
    setTickets([]);
  }

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

  return {
    tickets, setTickets, nextAfter, selectedId, setSelectedId, ticket, setTicket,
    run, setRun, events, setEvents, readError, setReadError, notice, setNotice,
    reason, setReason, uncertainDecision, setUncertainDecision,
    decisionSent, setDecisionSent, refresh, setRefresh,
    startKey, decisionAttempt, moreTickets,
    resetDomain, resetAll,
  } as const;
}
