import React, { useCallback, useState } from 'react';
import { createPortal } from 'react-dom';
import NestingWorkspace from '../features/nesting/NestingWorkspace';
import { NestingPortalContext } from '../features/nesting/PortalContext';
import styles from '../features/nesting/nesting.css?inline';
import { useAuth } from '../context/AuthContext';
import { useCompany } from '../context/CompanyContext';
import { usePermissions } from '../hooks/usePermissions';

/** A normal authenticated ERP route; only its presentation is isolated. */
export default function MaterialNesting() {
  const { user } = useAuth();
  const { canAll } = usePermissions();
  const { currentCompany } = useCompany();
  const owner = `${user?.id}:${currentCompany?.id ?? user?.company_id}`;
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const attach = useCallback((host: HTMLDivElement | null) => {
    if (!host) return;
    const shadow = host.shadowRoot ?? host.attachShadow({ mode: 'open' });
    let mount = shadow.querySelector<HTMLElement>('[data-nesting-mount]');
    if (!mount) {
      const sheet = document.createElement('style');
      sheet.textContent = styles;
      mount = document.createElement('div');
      mount.dataset.nestingMount = '';
      shadow.append(sheet, mount);
    }
    setTarget(mount);
  }, []);

  return (
    <div ref={attach} data-testid="material-nesting-host">
      {target &&
        createPortal(
          <NestingPortalContext.Provider value={target}>
            <NestingWorkspace
              key={owner}
              companyId={currentCompany?.id ?? user?.company_id}
              estimatorId={user?.id}
              canSaveDrafts={canAll(['purchasing:view', 'purchasing:create'])}
            />
          </NestingPortalContext.Provider>,
          target
        )}
    </div>
  );
}
