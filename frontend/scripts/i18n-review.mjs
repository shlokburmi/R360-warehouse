/**
 * Emit a side-by-side en/kn CSV for native review.
 *
 * The Kannada in this repo was authored without a Kannada speaker in the loop.
 * That is a real risk for operational text — a guard acts on these words — so
 * the strings have to be reviewable by someone who is not going to open a code
 * editor. This writes a spreadsheet: they correct column D, hand it back, and
 * `apply` writes it into kn.json without anyone touching TSX.
 *
 *   node scripts/i18n-review.mjs            -> i18n-review.csv
 *   node scripts/i18n-review.mjs apply FILE -> merge corrections into kn.json
 */

import { readFileSync, writeFileSync } from 'node:fs'
import path from 'node:path'

const root = path.resolve(import.meta.dirname, '..')
const file = (l) => path.join(root, 'src/i18n', `${l}.json`)
const load = (l) => JSON.parse(readFileSync(file(l), 'utf8'))

const flatten = (o, p = '') =>
  Object.entries(o).flatMap(([k, v]) =>
    v && typeof v === 'object' ? flatten(v, `${p}${k}.`) : [[`${p}${k}`, v]],
  )

const q = (s) => `"${String(s).replace(/"/g, '""')}"`

if (process.argv[2] === 'apply') {
  const csv = readFileSync(process.argv[3], 'utf8')
  // Parse only what this file emits: four quoted fields per line.
  const rows = [...csv.matchAll(/^"((?:[^"]|"")*)","((?:[^"]|"")*)","((?:[^"]|"")*)","((?:[^"]|"")*)"$/gm)]
  const kn = load('kn')
  let changed = 0
  for (const [, key, , , corrected] of rows.slice(1)) {
    const value = corrected.replace(/""/g, '"').trim()
    if (!value) continue
    const parts = key.split('.')
    let node = kn
    for (const p of parts.slice(0, -1)) node = node?.[p]
    if (!node) continue
    const last = parts.at(-1)
    if (node[last] !== value) {
      node[last] = value
      changed++
    }
  }
  writeFileSync(file('kn'), JSON.stringify(kn, null, 2) + '\n')
  console.log(`applied ${changed} correction(s) to kn.json`)
  console.log('now run: npm run i18n:check')
} else {
  const en = new Map(flatten(load('en')))
  const kn = new Map(flatten(load('kn')))
  const lines = ['"key","english","kannada (machine-authored)","kannada (corrected)"']
  for (const [key, value] of en) lines.push([key, value, kn.get(key) ?? '', ''].map(q).join(','))
  const out = path.join(root, 'i18n-review.csv')
  writeFileSync(out, lines.join('\n') + '\n')
  console.log(`${en.size} strings -> ${path.relative(root, out)}`)
}
