import React, { useMemo } from 'react';
import type { RunEvent } from '../api';

function date(value: string) { return new Date(value).toLocaleString('zh-CN'); }
function Json({ value }: { value: unknown }) { return <pre>{JSON.stringify(value, null, 2)}</pre>; }

interface EventTimelineProps {
  events: RunEvent[];
}

/** Ordered list of run audit events. */
const EventTimeline = React.memo(function EventTimeline({ events }: EventTimelineProps) {
  const sortedEvents = useMemo(() => [...events].sort((a, b) => a.id - b.id), [events]);

  return (
    <>
      <h3>调用轨迹 <small>{sortedEvents.length} 条</small></h3>
      <ol className="timeline">{sortedEvents.map(e => <li key={e.id}><strong>{e.event_type}</strong><small>{date(e.created_at)} · #{e.id}</small>
        <details><summary>事件详情</summary><Json value={e.payload_json} /></details></li>)}</ol>
    </>
  );
});

export default EventTimeline;
