import { useEffect, useState } from 'react';

/**
 * True when the user has requested reduced motion
 * (``prefers-reduced-motion: reduce``).  Consumers should disable
 * spring/transform transitions when this is true (WCAG 2.3.3 / motion
 * sensitivity).  SSR/jsdom-safe: defaults to false when matchMedia is absent.
 */
export default function useReducedMotion() {
  const [reduced, setReduced] = useState(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return false;
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  });

  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return undefined;
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (e) => setReduced(e.matches);
    // addEventListener is the modern API; fall back to addListener for older.
    if (mq.addEventListener) mq.addEventListener('change', onChange);
    else mq.addListener(onChange);
    return () => {
      if (mq.removeEventListener) mq.removeEventListener('change', onChange);
      else mq.removeListener(onChange);
    };
  }, []);

  return reduced;
}
