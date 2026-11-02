import React from 'react';
import type { Ticket } from '../api';

interface TicketDetailProps {
  ticket: Ticket | null;
  runId: string;
  busy: boolean;
  readError: string;
  isOperator: boolean;
  onStart: () => void;
  onViewExistingRun: () => void;
}

/** Shows ticket details and action buttons. */
const TicketDetail = React.memo(function TicketDetail({ ticket, runId, busy, readError, isOperator, onStart, onViewExistingRun }: TicketDetailProps) {
  return (
    <section><p className="eyebrow">工单详情</p><h1>{ticket?.id || '加载工单…'}</h1><h2>{ticket?.subject}</h2><p className="ticket-body">{ticket?.body}</p>
      <dl><dt>绑定订单</dt><dd>{ticket?.bound_order_id || '未绑定'}</dd><dt>工单状态</dt><dd>{ticket?.status === 'OPEN' ? '打开' : ticket?.status}</dd></dl>
      {!runId && <><button className="primary" onClick={onStart} disabled={busy || !isOperator || !ticket || !!readError}>{busy ? '提交中…' : '开始诊断'}</button>
        {ticket?.active_run_id && <button onClick={onViewExistingRun} disabled={busy}>查看现有任务</button>}
        {!isOperator && <p className="muted">仅客服操作员可以发起诊断。</p>}</>}
      {runId && <><p className="muted">任务 ID</p><code className="break">{runId}</code><p className="muted">刷新或复制当前网址可恢复此任务。</p></>}
    </section>
  );
});

export default TicketDetail;
