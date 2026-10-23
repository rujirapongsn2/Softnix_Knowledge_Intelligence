// Layout for the status page's error chart: one bar per hour or day, stacked by error group, failed below retried.

export const ERROR_GROUP_COLORS = {
  retrieval_engine: "#2786C2",
  ai_provider: "#D97706",
  ocr_extraction: "#7C3AED",
  metadata: "#0F9D8A",
  index_cleanup: "#DC2626",
  other: "#6B7280",
};

// A round axis top at or above the tallest bar: 1, 2, 2.5 or 5 times a power of ten. Counts are whole, so 2.5
// is reached only from 25 up.
export function axisMax(value) {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  return [1, 2, 2.5, 5, 10].map(step => step * power).find(top => top >= value);
}

export function stackBars(timeline, includeRetried) {
  const outcomes = includeRetried ? ["failed", "retried"] : ["failed"];
  const bars = (timeline?.series || []).map(bar => {
    let top = 0;
    const segments = [];
    for (const outcome of outcomes) {
      for (const group of timeline.groups) {
        const value = bar[outcome][group];
        if (!value) continue;
        segments.push({group, outcome, value, from: top, to: top + value});
        top += value;
      }
    }
    return {start: bar.start, total: top, segments};
  });
  return {bars, max: axisMax(Math.max(0, ...bars.map(bar => bar.total)))};
}

// Every how many bars a label fits under them.
export const labelEvery = barCount => (barCount <= 7 ? 1 : barCount <= 24 ? 3 : barCount <= 30 ? 5 : 15);

// A bar's start is a local ISO time such as 2026-10-23T14:00:00+07:00: hourly bars are labelled 14:00, daily ones 23/10.
export function barLabel(start, bucket) {
  const [date, clock] = start.split("T");
  const [, month, day] = date.split("-");
  return bucket === "hour" ? clock.slice(0, 5) : `${day}/${month}`;
}

export function barTitle(start, bucket) {
  const [date] = start.split("T");
  const [, month, day] = date.split("-");
  return bucket === "hour" ? `${day}/${month} ${barLabel(start, bucket)}` : `${day}/${month}`;
}

export function groupTotals(timeline, includeRetried) {
  const totals = Object.fromEntries((timeline?.groups || []).map(group => [group, 0]));
  for (const bar of timeline?.series || []) {
    for (const group of timeline.groups) totals[group] += bar.failed[group] + (includeRetried ? bar.retried[group] : 0);
  }
  return totals;
}
