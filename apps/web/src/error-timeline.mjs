// Layout for the status page's error chart: one bar per day, stacked by error group, failed below retried.

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

export function stackDays(timeline, includeRetried) {
  const outcomes = includeRetried ? ["failed", "retried"] : ["failed"];
  const days = (timeline?.series || []).map(day => {
    let top = 0;
    const segments = [];
    for (const outcome of outcomes) {
      for (const group of timeline.groups) {
        const value = day[outcome][group];
        if (!value) continue;
        segments.push({group, outcome, value, from: top, to: top + value});
        top += value;
      }
    }
    return {date: day.date, total: top, segments};
  });
  return {days, max: axisMax(Math.max(0, ...days.map(day => day.total)))};
}

// Every how many days a date label fits under the bars.
export const labelEvery = dayCount => (dayCount <= 7 ? 1 : dayCount <= 30 ? 5 : 15);

export function groupTotals(timeline, includeRetried) {
  const totals = Object.fromEntries((timeline?.groups || []).map(group => [group, 0]));
  for (const day of timeline?.series || []) {
    for (const group of timeline.groups) totals[group] += day.failed[group] + (includeRetried ? day.retried[group] : 0);
  }
  return totals;
}
