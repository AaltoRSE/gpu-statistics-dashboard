/* Shared batch-progress helpers for the tabs' long fetches.
 *
 * batchText renders the "prefix: batch k of M (n failed)…" chip line and
 * pollProgress is the best-effort once-a-second poll behind it. The
 * Partitions tabs (wait history, VRAM enrichment) and the Groups tab
 * (member lists, owner classification) both drive their loading chips
 * with these — one implementation here, so the chips read the same and
 * the poll's guarantees (404 self-stop, token gating, idle reset) hold
 * everywhere. */

// Plain-text batch progress line. The ellipsis is the literal character:
// an escaped "&hellip;" would render as the visible word "hellip".
export function batchText(prefix, done, total, failed) {
  return prefix + ": batch " + done + " of " + total
    + (failed ? " (" + failed + " failed)" : "") + "…";
}

// Best-effort once-a-second progress polling for a batched fetch: calls
// onUpdate with each poll's batch state while isCurrent holds, calls
// onIdle when the endpoint answers but has no in-flight batch (the phase
// finished — its key is popped on completion — or the dump was cached, or
// the batches are all done and the request is in its slower, unbatched
// remainder), stops itself after a 404 (a stale backend without the
// progress route must not be hammered every second), ignores any other
// error, and returns the cleanup function every success/error/supersession
// path must call. The token gate keeps a late poll from a superseded
// request from overwriting the new request's reset label.
export function pollProgress(url, isCurrent, onUpdate, onIdle) {
  const pollTimer = setInterval(async () => {
    try {
      const resp = await fetch(url);
      if (resp.status === 404) {
        clearInterval(pollTimer);
        return;
      }
      if (!resp.ok || !isCurrent()) return;
      const prog = await resp.json();
      if (!isCurrent()) return;
      if (prog && prog.total && prog.done < prog.total) onUpdate(prog);
      else if (onIdle) onIdle();
    } catch (_) { /* progress is best-effort; the request decides */ }
  }, 1000);
  return () => clearInterval(pollTimer);
}
