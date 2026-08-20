/**
 * Verify the translation catalogs against the code that uses them.
 *
 * Three failure modes, all of which have bitten this codebase already or would:
 *
 *  1. A `t('some.key')` whose key is missing from en.json. i18next renders the
 *     key itself, so the screen shows "dashboard.boxes_held" to an operator.
 *     Nothing throws, nothing fails to build — it just ships.
 *  2. A key in en.json with no counterpart in kn.json. The Kannada user silently
 *     gets English for that one string.
 *  3. Placeholders that disagree between languages: if en has {{count}} and kn
 *     does not, the number vanishes from the Kannada sentence.
 */

import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

const root = path.resolve(import.meta.dirname, '..')
const load = (l) => JSON.parse(readFileSync(path.join(root, 'src/i18n', `${l}.json`), 'utf8'))

const flatten = (obj, prefix = '') =>
  Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === 'object' ? flatten(v, `${prefix}${k}.`) : [[`${prefix}${k}`, v]],
  )

const en = new Map(flatten(load('en')))
const kn = new Map(flatten(load('kn')))

const walk = (dir) =>
  readdirSync(dir).flatMap((f) => {
    const full = path.join(dir, f)
    return statSync(full).isDirectory() ? walk(full) : full.match(/\.tsx?$/) ? [full] : []
  })

const problems = []

// 1. keys used in code but absent from the catalog
for (const file of walk(path.join(root, 'src'))) {
  const src = readFileSync(file, 'utf8')
  for (const m of src.matchAll(/\bt\(\s*'([a-z0-9_]+(?:\.[a-z0-9_]+)+)'/gi)) {
    const key = m[1]
    // A `defaultValue` makes a dynamic key legitimate — StatusChip builds its key
    // from a Postgres enum, so not every value can be enumerated here.
    const dynamic = src.slice(m.index, m.index + 200).includes('defaultValue')
    if (!en.has(key) && !dynamic) {
      problems.push(`${path.relative(root, file)}: t('${key}') is not in en.json`)
    }
  }
}

// 2. + 3. catalog parity and placeholder parity
for (const [key, value] of en) {
  if (!kn.has(key)) {
    problems.push(`kn.json: missing '${key}'`)
    continue
  }
  const vars = (s) => [...String(s).matchAll(/{{\s*(\w+)\s*}}/g)].map((m) => m[1]).sort().join(',')
  if (vars(value) !== vars(kn.get(key))) {
    problems.push(`'${key}': placeholders differ — en has [${vars(value)}], kn has [${vars(kn.get(key))}]`)
  }
}
for (const key of kn.keys()) if (!en.has(key)) problems.push(`en.json: missing '${key}'`)

if (problems.length) {
  console.error(`i18n check failed (${problems.length}):`)
  for (const p of problems) console.error('  ' + p)
  process.exit(1)
}
console.log(`i18n ok — ${en.size} keys, en/kn in parity`)
