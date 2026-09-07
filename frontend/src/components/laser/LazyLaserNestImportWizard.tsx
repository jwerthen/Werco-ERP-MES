import React, { lazy, Suspense, useState } from 'react';
import type { ComponentProps } from 'react';
import type LaserNestImportWizard from './LaserNestImportWizard';
import { ErrorBoundary } from '../ErrorBoundary';
import { Modal } from '../ui/Modal';
import { Button } from '../ui/Button';

type Props = ComponentProps<typeof LaserNestImportWizard>;

/** The list does not download the import workflow until a planner opens it. */
export default function LazyLaserNestImportWizard(props: Props) {
  const [{ attempt, Wizard }, setLoader] = useState(() => ({
    attempt: 0,
    Wizard: lazy(() => import('./LaserNestImportWizard')),
  }));
  // A new lazy instance on retry clears React's cached rejected import promise.
  const retryLoading = () =>
    setLoader(previous => ({
      attempt: previous.attempt + 1,
      Wizard: lazy(() => import('./LaserNestImportWizard')),
    }));
  if (!props.open) return null;

  return (
    <ErrorBoundary
      key={attempt}
      name="Nest import loading"
      fallback={
        <Modal open onClose={props.onClose} ariaLabel="Import Nest Package">
          <h2 className="text-lg font-semibold">Import Nest Package</h2>
          <p role="alert" className="my-4 text-sm text-amber-200">
            The import tools could not load. Your work-order list is still available.
          </p>
          {attempt > 0 && (
            <p className="my-4 text-sm text-slate-300">
              Your browser may have cached the failed download. Reload this page, then open the import again.
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="secondary" onClick={props.onClose}>
              Cancel
            </Button>
            {attempt === 0 ? (
              <Button onClick={retryLoading}>Retry loading</Button>
            ) : (
              <Button onClick={() => window.location.reload()}>Reload page</Button>
            )}
          </div>
        </Modal>
      }
    >
      <Suspense
        fallback={
          <Modal open onClose={props.onClose} ariaLabel="Import Nest Package">
            <h2 className="text-lg font-semibold">Import Nest Package</h2>
            <p role="status" className="my-4 text-sm text-slate-300">
              Loading nest import…
            </p>
            <div className="flex justify-end">
              <Button variant="secondary" onClick={props.onClose}>
                Cancel
              </Button>
            </div>
          </Modal>
        }
      >
        <Wizard {...props} />
      </Suspense>
    </ErrorBoundary>
  );
}
