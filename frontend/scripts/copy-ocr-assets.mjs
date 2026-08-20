/**
 * Stage the Tesseract and pdf.js runtimes into public/tesseract/.
 *
 * Why a copy step instead of importing these through Vite:
 *
 * The `.wasm.js` loader fetches its `.wasm` twin by *relative* name at runtime.
 * Importing the loader with `?url` lets Vite hash it into `dist/assets/`, which
 * breaks that sibling relationship — the loader then asks for a file that isn't
 * where it expects, and OCR fails only in the production build. Copying the pair
 * into `public/` keeps them adjacent and untouched.
 *
 * Why not commit them instead: this is ~8MB of binaries. Sourcing them from
 * node_modules keeps the versions pinned by package.json rather than by whoever
 * last remembered to re-download a blob, and keeps the git history free of them.
 * `public/tesseract/` is gitignored for that reason.
 *
 * Only the LSTM cores are copied. The full cores add a legacy engine this app
 * never asks for — 3.3-3.9MB each for nothing.
 */

import { copyFile, mkdir, readFile, readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dest = path.join(root, 'public', 'tesseract')

const files = [
  ['node_modules/tesseract.js/dist/worker.min.js', 'worker.min.js'],
  // pdf.js parses PDFs on a worker thread and resolves that worker by URL at
  // runtime. Same reasoning as the Tesseract assets: served from our own origin
  // so a warehouse with no wifi can still open a challan, and copied rather than
  // committed so the version is pinned by package.json.
  ['node_modules/pdfjs-dist/build/pdf.worker.min.mjs', 'pdf.worker.min.mjs'],
  // The traineddata. `best_int` (3.1MB) rather than the 10MB standard model:
  // this only ever reads a 16-character alphanumeric run, and the accuracy
  // difference on that does not pay for 7MB on a warehouse phone.
  ['node_modules/@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz', 'eng.traineddata.gz'],
]

// Every LSTM core variant, because tesseract.js probes the browser for SIMD and
// relaxed-SIMD support and requests whichever it settles on. Shipping only the one
// this laptop would pick is how you discover, at the gate, that the tablet picked
// differently.
//
// It has to be the `.wasm.js` form. When `corePath` names a directory,
// tesseract.js asks for `tesseract-core-<variant>-lstm.wasm.js` — the build with
// the binary inlined — and nothing else. An earlier version of this script copied
// the 88KB `.js` loader plus its `.wasm` twin instead, reasoning that 4.1MB of
// base64 was wasteful. It is wasteful, and it is also what the library requests:
// the `.wasm.js` URL 404'd, Vite's SPA fallback answered with index.html at status
// 200, `importScripts` ran HTML as JavaScript, and the engine hung on "loading"
// forever with no error. Correctness first.
const coreDir = 'node_modules/tesseract.js-core'
const cores = (await readdir(path.join(root, coreDir))).filter((f) =>
  /-lstm\.wasm\.js$/.test(f),
)

await mkdir(dest, { recursive: true })

for (const [from, to] of [...files, ...cores.map((c) => [path.join(coreDir, c), c])]) {
  await copyFile(path.join(root, from), path.join(dest, to))
}

// ---------------------------------------------------------------------------
// Verify against what tesseract.js will actually ask for.
//
// The filenames are decided inside the library, not by us, and getting them wrong
// does not fail the build — it 404s at runtime, Vite answers the SPA fallback with
// index.html at status 200, and the engine hangs on "loading" with no error. So
// the names are read back out of the library's own resolver and checked. If
// tesseract.js changes them in a future version, this fails here instead of on a
// warehouse tablet.
const resolver = await readFile(
  path.join(root, coreDir, '..', 'tesseract.js/src/worker-script/browser/getCore.js'),
  'utf8',
)
const wanted = [...resolver.matchAll(/\/(tesseract-core-[a-z-]*lstm\.wasm\.js)/g)].map((m) => m[1])
const staged = new Set(await readdir(dest))
const missing = wanted.filter((w) => !staged.has(w))

if (wanted.length === 0) {
  throw new Error(
    'Could not read core filenames out of tesseract.js getCore.js — the library layout changed.',
  )
}
if (missing.length > 0) {
  throw new Error(`tesseract.js will request these, but they were not staged: ${missing.join(', ')}`)
}

console.log(`ocr assets: ${files.length + cores.length} files → public/tesseract/`)
console.log(`  verified ${wanted.length} core filenames against tesseract.js getCore.js`)
