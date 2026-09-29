import { useEffect, useRef } from "react";

/**
 * Call `refresh` every `intervalMs` while the page is visible, and right away
 * when the window regains focus or the page becomes visible again. A hidden
 * page does not poll; `enabled: false` (or unmounting) stops polling.
 */
export function useVisiblePolling(refresh: () => void, intervalMs: number, enabled: boolean) {
  const latest = useRef(refresh);
  useEffect(() => {
    latest.current = refresh;
  }, [refresh]);
  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      if (!document.hidden) latest.current();
    };
    const timer = window.setInterval(tick, intervalMs);
    window.addEventListener("focus", tick);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", tick);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [enabled, intervalMs]);
}
