import { useEffect, useRef } from "react";

/** Call `callback` every `ms` milliseconds while mounted; the latest callback is always used. */
export function useInterval(callback: () => void, ms: number): void {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback;
  });
  useEffect(() => {
    const id = window.setInterval(() => saved.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}
