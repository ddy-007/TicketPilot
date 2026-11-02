import React, { useCallback } from 'react';
import type { TicketSummary } from '../api';

interface TicketListProps {
  tickets: TicketSummary[];
  nextAfter: string | null;
  busy: boolean;
  error: string;
  readError: string;
  onSelect: (id: string) => void;
  onMore: () => void;
}

/** Grid of ticket summaries with pagination. */
const TicketList = React.memo(function TicketList({ tickets, nextAfter, busy, error, readError, onSelect, onMore }: TicketListProps) {
  return (
    <section><h1>待处理工单</h1><p className="muted">选择工单，查看绑定订单后发起诊断。</p>
      <div className="ticket-list">{tickets.map(t => <TicketItem key={t.id} ticket={t} onSelect={onSelect} />)}</div>
      {nextAfter && <button onClick={onMore} disabled={busy}>加载更多工单</button>}
      {!tickets.length && !error && !readError && <p>暂无工单或正在加载…</p>}
    </section>
  );
});

interface TicketItemProps {
  ticket: TicketSummary;
  onSelect: (id: string) => void;
}

const TicketItem = React.memo(function TicketItem({ ticket: t, onSelect }: TicketItemProps) {
  const handleClick = useCallback(() => onSelect(t.id), [onSelect, t.id]);
  return (
    <button className="ticket" onClick={handleClick}>
      <strong>{t.id}</strong><span>{t.subject}</span>
      <small>绑定订单 {t.bound_order_id || '未绑定'} · {t.status === 'OPEN' ? '打开' : '关闭'}{t.active_run_id ? ' · 存在运行任务' : ''}</small>
    </button>
  );
});

export default TicketList;
