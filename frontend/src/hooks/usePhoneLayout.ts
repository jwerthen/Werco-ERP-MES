import { useEffect, useState } from 'react';

/** Match the application's md breakpoint, including rotation while working. */
export function usePhoneLayout() {
  const [phone, setPhone] = useState(() => window.matchMedia('(max-width: 767px)').matches);
  useEffect(() => {
    const query = window.matchMedia('(max-width: 767px)');
    const update = () => setPhone(query.matches);
    update();
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);
  return phone;
}
