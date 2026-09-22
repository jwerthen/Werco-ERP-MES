import { useCallback, useEffect, useRef, useState } from 'react';
import { getHankSessionScope, subscribeHankSession } from './hankSession';

/** Pins all reads and writes to the actor/company that opened this workflow. */
export function useHankSessionGuard() {
  const [scope] = useState(getHankSessionScope);
  const [changed, setChanged] = useState(false);
  const mounted = useRef(true);
  const controllers = useRef(new Set<AbortController>());
  const current = useCallback(() => mounted.current && scope !== null && scope === getHankSessionScope(), [scope]);
  useEffect(() => {
    mounted.current = true;
    const abortAll = () => {
      controllers.current.forEach(controller => controller.abort());
      controllers.current.clear();
    };
    const unsubscribe = subscribeHankSession(() => {
      if (scope !== getHankSessionScope()) {
        abortAll();
        setChanged(true);
      }
    });
    return () => {
      mounted.current = false;
      abortAll();
      unsubscribe();
    };
  }, [scope]);
  const controller = useCallback(() => {
    const next = new AbortController();
    if (!current()) next.abort();
    else controllers.current.add(next);
    return next;
  }, [current]);
  const release = useCallback((value: AbortController) => {
    controllers.current.delete(value);
  }, []);
  return { current, controller, release, changed };
}
