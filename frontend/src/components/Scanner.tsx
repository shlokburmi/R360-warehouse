import { Suspense, lazy } from 'react'

/**
 * Lazily-loaded wrapper around the camera scanner.
 *
 * The ZXing decoder is by far the largest dependency in the app — bigger than
 * React and the router combined. Loading it eagerly would put it in the initial
 * bundle for everyone, including an Ops Manager who only ever opens the
 * dashboard on a desktop.
 *
 * Splitting it out means the gate entry form, the approval queue and the
 * dashboard all load without it, and the scanner is fetched at the moment
 * someone actually opens a scanning page. On a warehouse phone that is the
 * difference between the login screen appearing quickly and appearing
 * eventually.
 */
const QrScannerImpl = lazy(() =>
  import('./QrScanner').then((m) => ({ default: m.QrScanner })),
)

type Props = {
  onScan: (code: string) => void
  debounceMs?: number
  paused?: boolean
}

export function Scanner(props: Props) {
  return (
    <Suspense
      fallback={
        <div className="flex h-48 items-center justify-center rounded-2xl bg-slate-100 dark:bg-slate-800">
          <span className="text-lg text-slate-500">Loading scanner…</span>
        </div>
      }
    >
      <QrScannerImpl {...props} />
    </Suspense>
  )
}
