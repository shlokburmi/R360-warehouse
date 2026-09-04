import { Component, type ReactNode } from 'react'

type Props = { children: ReactNode }
type State = { failed: boolean }

/**
 * The last line of defence against a blank white screen.
 *
 * Without this, an uncaught render error anywhere in the tree — most
 * plausibly a lazily-loaded chunk failing to fetch (Scanner.tsx loads the
 * ~large ZXing bundle the moment someone opens a scanning page, which is
 * exactly when a phone might be on bad signal) — unmounts the whole app
 * with no explanation and no way back except knowing to reload manually.
 *
 * Deliberately a single boundary around the whole app rather than one per
 * page: a chunk-load failure on one page is not this app's fault and not
 * recoverable by re-rendering a smaller subtree — the fix is the same
 * either way (reload once the connection is better), so there is nothing a
 * more granular boundary would do differently here.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  render() {
    if (this.state.failed) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-slate-50 p-6 text-center dark:bg-slate-950">
          <p className="text-xl font-bold text-slate-900 dark:text-slate-100">
            Something didn't load correctly.
          </p>
          <p className="max-w-sm text-base text-slate-600 dark:text-slate-400">
            This usually means a weak connection interrupted loading part of the
            app. Reload once your signal improves.
          </p>
          <button
            type="button"
            className="btn-primary"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
        </div>
      )
    }

    return this.props.children
  }
}
