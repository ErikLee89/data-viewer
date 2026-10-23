/* View-dependent M4 sampling. Original arrays are never modified. */
(function (root) {
  "use strict";
  function lowerBound(times, value, upper = false) {
    let lo = 0, hi = times.length;
    while (lo < hi) {
      const mid = lo + Math.floor((hi - lo) / 2);
      if (times[mid] < value || (upper && times[mid] === value)) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }
  function sample(times, values, start, end, width) {
    if (!times.length) return [];
    const buckets = Math.max(1, Math.floor(width / 2));
    const first = Math.max(0, lowerBound(times, start) - 1);
    const last = Math.min(times.length, lowerBound(times, end, true) + 1);
    const out = [];
    const add = index => out.push([times[index], values[index] == null || !Number.isFinite(values[index]) ? null : values[index]]);
    // Two neighboring points retain lines that cross the viewport boundary.
    if (last - first <= buckets * 4) {
      for (let i = first; i < last; i++) add(i);
      return out;
    }
    const span = end - start || 1;
    let bucket = -Infinity, head = -1, tail = -1, min = -1, max = -1;
    const flush = () => {
      if (head < 0) return;
      const indices = [...new Set([head, min, max, tail])].sort((a, b) => a - b);
      indices.forEach(add);
      head = tail = min = max = -1;
    };
    let inGap = false;
    for (let i = first; i < last; i++) {
      const value = values[i];
      if (value == null || !Number.isFinite(value)) {
        flush();
        if (!inGap) add(i);
        inGap = true;
        continue;
      }
      const nextBucket = Math.floor((times[i] - start) / span * buckets);
      if (nextBucket !== bucket || inGap) {
        flush();
        bucket = nextBucket;
      }
      inGap = false;
      if (head < 0) head = min = max = i;
      tail = i;
      if (value < values[min]) min = i;
      if (value > values[max]) max = i;
    }
    flush();
    return out;
  }
  root.TimeSeriesSampling = { sample, lowerBound };
})(typeof globalThis !== "undefined" ? globalThis : this);
