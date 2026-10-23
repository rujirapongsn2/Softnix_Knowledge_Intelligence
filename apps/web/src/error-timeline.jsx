import React from "react";
import {ERROR_GROUP_COLORS, groupTotals, labelEvery, stackDays} from "./error-timeline.mjs";

const WIDTH = 720, HEIGHT = 220, LEFT = 34, RIGHT = 8, TOP = 10, BOTTOM = 26;

const dayLabel = date => { const [, month, day] = date.split("-"); return `${day}/${month}`; };

export function ErrorTimelineChart({timeline, includeRetried, t}) {
  const {days, max} = stackDays(timeline, includeRetried);
  const plotWidth = WIDTH - LEFT - RIGHT, plotHeight = HEIGHT - TOP - BOTTOM;
  const slot = plotWidth / Math.max(days.length, 1), barWidth = Math.max(2, slot * 0.7);
  const y = value => TOP + plotHeight - (value / max) * plotHeight;
  const every = labelEvery(days.length);
  const totals = groupTotals(timeline, includeRetried);
  const ticks = max % 2 === 0 ? [0, max / 2, max] : [0, max];
  return <div className="error-timeline">
    <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={t("systemStatus.timeline.aria")}>
      {ticks.map(value => <g key={value}>
        <line className="error-timeline-grid" x1={LEFT} x2={WIDTH - RIGHT} y1={y(value)} y2={y(value)}/>
        <text className="error-timeline-axis" x={LEFT - 6} y={y(value) + 4} textAnchor="end">{value}</text>
      </g>)}
      {days.map((day, index) => {
        const x = LEFT + index * slot + (slot - barWidth) / 2;
        return <g key={day.date}>
          {day.segments.map(segment => <rect key={`${segment.outcome}-${segment.group}`} x={x} width={barWidth} y={y(segment.to)}
            height={y(segment.from) - y(segment.to)} fill={ERROR_GROUP_COLORS[segment.group]} opacity={segment.outcome === "retried" ? 0.4 : 1}>
            <title>{`${dayLabel(day.date)} · ${t(`systemStatus.errorGroup.${segment.group}`)} · ${t(`systemStatus.timeline.${segment.outcome}`)}: ${segment.value}`}</title>
          </rect>)}
          {index % every === 0 && <text className="error-timeline-axis" x={x + barWidth / 2} y={HEIGHT - 8} textAnchor="middle">{dayLabel(day.date)}</text>}
        </g>;
      })}
    </svg>
    <ul className="error-timeline-legend">
      {timeline.groups.map(group => <li key={group}><span style={{background: ERROR_GROUP_COLORS[group]}}/>{t(`systemStatus.errorGroup.${group}`)} <b>{totals[group]}</b></li>)}
    </ul>
    {includeRetried && <p className="section-copy error-timeline-note">{t("systemStatus.timeline.legendNote")}</p>}
  </div>;
}
