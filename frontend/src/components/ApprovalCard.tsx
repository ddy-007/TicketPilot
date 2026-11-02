import React, { useCallback, useEffect, useState } from 'react';
import type { Approval, Run } from '../api';
import { terminal } from '../flow';

function date(value: string) { return new Date(value).toLocaleString('zh-CN'); }
function Json({ value }: { value: unknown }) { return <pre>{JSON.stringify(value, null, 2)}</pre>; }

interface ApprovalCardProps {
  run: Run | null;
  isApprover: boolean;
  readError: string;
  busy: boolean;
  reason: string;
  setReason: React.Dispatch<React.SetStateAction<string>>;
  uncertainDecision: string | null;
  decisionSent: boolean;
  onDecide: (approval: Approval, decision: string) => void;
}

/** Approval card for human review with approve/reject actions. */
const ApprovalCard = React.memo(function ApprovalCard({
  run, isApprover, readError, busy, reason, setReason, uncertainDecision, decisionSent, onDecide,
}: ApprovalCardProps) {
  const approval = run?.approval;
  const [, updateClock] = useState(0);
  const deadline = approval?.decision === 'PENDING' ? Date.parse(approval.expires_at) : null;
  useEffect(() => {
    if (deadline === null) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () => {
      clearTimeout(timer);
      updateClock(value => value + 1);
      const remaining = deadline - Date.now();
      if (remaining > 0) timer = setTimeout(check, Math.min(remaining, 2_147_483_647));
    };
    check();
    window.addEventListener('focus', check);
    document.addEventListener('visibilitychange', check);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('focus', check);
      document.removeEventListener('visibilitychange', check);
    };
  }, [deadline]);
  const expired = approval ? Date.now() >= Date.parse(approval.expires_at) : false;
  const mayDecide = isApprover && run?.status === 'WAITING_APPROVAL' && approval?.decision === 'PENDING' && !expired && !readError;

  const handleReasonChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => setReason(e.target.value), [setReason]);
  const handleApprove = useCallback(() => { if (approval) onDecide(approval, 'APPROVED'); }, [approval, onDecide]);
  const handleReject = useCallback(() => { if (approval) onDecide(approval, 'REJECTED'); }, [approval, onDecide]);
  const handleRetry = useCallback(() => { if (approval && uncertainDecision) onDecide(approval, uncertainDecision); }, [approval, uncertainDecision, onDecide]);

  return (
    <section><p className="eyebrow">人工控制</p><h2>审批卡片</h2>{approval ? <>
      <p className="badge">{approval.decision === 'PENDING' ? (expired ? '审批已过期' : '等待决定') : { APPROVED: '已批准', REJECTED: '已拒绝', EXPIRED: '已过期' }[approval.decision] || approval.decision}</p>
      <dl><dt>操作</dt><dd>{approval.plan.tool}</dd><dt>订单</dt><dd>{approval.plan.args.order_id}</dd><dt>预期版本</dt><dd>{approval.plan.args.expected_version}</dd>
        <dt>失败码</dt><dd>{approval.plan.args.expected_error_code}</dd><dt>有效期</dt><dd>{date(approval.expires_at)}</dd></dl>
      <p>{approval.plan.reason}</p><p className="notice">{approval.plan.expected_effect}</p>
      <p>证据：{approval.plan.evidence_ids.join('、')}</p><details><summary>完整计划与服务端哈希</summary><Json value={approval.plan} /><code className="break">{approval.plan_hash}</code></details>
      {approval.reason && <p>审批理由：{approval.reason}</p>}
      {!isApprover && <p className="muted">当前为客服操作员，无审批权限。请切换审批员身份。</p>}
      {approval.decision === 'PENDING' && <><label htmlFor="reason">审批理由（可选，请勿填写凭证或个人信息）</label><textarea id="reason" value={reason} onChange={handleReasonChange} maxLength={500} disabled={!mayDecide || busy || !!uncertainDecision} />
        <div className="actions"><button className="primary" onClick={handleApprove} disabled={!mayDecide || busy || !!uncertainDecision || decisionSent}>批准履约重试</button>
          <button onClick={handleReject} disabled={!mayDecide || busy || !!uncertainDecision || decisionSent}>拒绝</button></div>
        {uncertainDecision && <button onClick={handleRetry} disabled={busy}>重试原审批决定</button>}</>}
    </> : <p className="muted">{run && terminal.has(run.status) ? '本轮没有待审批提案。' : '完成诊断后，符合规则的提案将在此展示。'}</p>}</section>
  );
});

export default ApprovalCard;
