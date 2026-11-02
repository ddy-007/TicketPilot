import React, { useMemo } from 'react';
import type { Run, RunEvent } from '../api';
import { resultTitle, statusLabels } from '../flow';
import EventTimeline from './EventTimeline';

function Json({ value }: { value: unknown }) { return <pre>{JSON.stringify(value, null, 2)}</pre>; }

interface RunViewProps {
  run: Run | null;
  events: RunEvent[];
  readError: string;
}

/** Shows run status, result, evidence, and event timeline. */
const RunView = React.memo(function RunView({ run, events, readError }: RunViewProps) {
  const title = useMemo(() => run ? resultTitle(run) : '加载运行状态…', [run]);
  const statusLabel = useMemo(() => run ? (statusLabels[run.status as keyof typeof statusLabels] || run.status) : '读取中', [run]);

  return (
    <section><p className="eyebrow">诊断与执行</p><h2>{title}</h2>
      {readError && <p className="muted">部分信息读取失败，当前内容可能不是最新状态。请刷新核对。</p>}
      <p className="badge">{statusLabel}</p>
      {run?.result_json && <div className="result"><p>{run.result_json.summary}</p><code>{run.result_json.reason_code}</code>{run.result_json.warnings?.map(w => <p key={w}>{w}</p>)}</div>}
      <EventTimeline events={events} />
      <h3>证据 <small>{run?.evidence.length || 0} 条</small></h3>{run?.evidence.map(e => <details key={e.evidence_id}><summary>{e.tool_name} · {e.ok ? '成功' : '失败'} · {e.evidence_id}</summary><Json value={e} /></details>)}
    </section>
  );
});

export default RunView;
