/**
 * Stage the Tesseract runtime into public/tesseract/.
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

import { copyFile, mkdir, readdir } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const dest = path.join(root, 'public', 'tesseract')

const files = [
  ['node_modules/tesseract.js/dist/worker.min.js', 'worker.min.js'],
  // The traineddata. `best_int` (3.1MB) rather than the 10MB standard model:
  // this only ever reads a 16-character alphanumeric run, and the accuracy
  // difference on that does not pay for 7MB on a warehouse phone.
  ['node_modules/@tesseract.js-data/eng/4.0.0_best_int/eng.traineddata.gz', 'eng.traineddata.gz'],
]

// Every LSTM core variant, because tesseract.js probes the browser for SIMD and
// relaxed-SIMD support and requests whichever it settles on. Shipping only the
// one this laptop would pick is how you discover, at the gate, that the tablet
// picked differently.
//
// The pair wanted is the 88KB `.js` loader plus its 3.1MB `.wasm`. Note the
// third file the package ships, `<name>.wasm.js`: that is a 4.1MB standalone
// build with the binary inlined as base64, for environments that cannot fetch a
// .wasm at all. Taking it *as well* triples the payload to no purpose — copying
// `.wasm.js` by mistake is what turned this directory into 25MB on the first
// attempt.
const coreDir = 'node_modules/tesseract.js-core'
const cores = (await readdir(path.join(root, coreDir))).filter(
  (f) => f.includes('lstm') && (f.endsWith('.wasm') || /-lstm\.js$/.test(f)),
)

await mkdir(dest, { recursive: true })

for (const [from, to] of [...files, ...cores.map((c) => [path.join(coreDir, c), c])]) {
  await copyFile(path.join(root, from), path.join(dest, to))
}

console.log(`ocr assets: ${files.length + cores.length} files → public/tesseract/`)
