// The concrete details an output carries that the model can't know without it: names, paths and
// values from the project, not plain words or standard-library calls it knows anyway. Used for the
// index each shortening note carries (what was removed, so the model re-checks instead of
// guessing), and by the benchmark's hindsight test.

export const FACT = /[A-Za-z_][A-Za-z0-9_./-]{5,}(?:=[^\s,;'"]{1,24})?|\b\d{4,}\b/g
const STDLIB = /^(time|json|os|sys|re|io|math|random|shutil|subprocess|threading|logging|pathlib|Path|datetime|collections|itertools|functools|typing|unittest|self\.assert|Object|Date|Math|JSON|Promise|Array|String|Number|console|process|fs|path|Buffer|Map|Set|Reflect|Symbol|window|document)\./
// How much of a note may name what was removed (only what the conversation no longer shows anywhere
// else). Measured live on the benchmark session (facts lost / outputs needed later / tokens after):
// no index 27-32 / 12 / 108k; 600 chars 7 / 2 / 114k; 1200 chars 2 / 1 / 116k.
// With names given once across notes (newest note first), on 6 synthetic sessions (smaller / facts lost):
// 900 chars 88% / 76; 1200 chars 88-89% / 64; 1600 chars 88% / 49. A big output holds more names than a
// small one: 15% of its size, within 1600-8000 chars (with live Jev and stubThreshold 0.22, on 6 synthetic
// sessions plus 6 unseen ones: needed outputs cut 0/158 and 0/132, against 1/158 and 1/132 at a flat 1600).
export const INDEX_CHARS = 1600
export const INDEX_SHARE = 0.15
export const INDEX_MAX = 8000

/** How many characters a note may spend naming what `chars` of removed output held. */
export function indexBudget(chars: number): number {
  return Math.min(INDEX_MAX, Math.max(INDEX_CHARS, Math.round(chars * INDEX_SHARE)))
}
const DEFINES = /\b(?:def|class|function|interface|type|enum|struct|fn|func|const|let|var)\s+([A-Za-z_][\w]*)/g

/** Identifier-like (a _ . / = or digit, or mixedCase), not a standard-library call. */
export function projectFact(fact: string): boolean {
  return (/[_./=\d]|[a-z][A-Z]/.test(fact) && !STDLIB.test(fact)) || /^[A-Z][A-Z0-9_]{5,}$/.test(fact)
}

export function factsIn(text: string): Set<string> {
  return new Set((text.match(FACT) ?? []).filter(projectFact))
}

/**
 * The details of `text` worth naming in a note, best first, within `budget` characters joined by
 * ", ": what it defines, then what the conversation also mentions (`known`), then the rest in order;
 * none that `visible` holds (what the conversation still shows after the compaction).
 */
export function factIndex(text: string, known: ReadonlySet<string>, budget = INDEX_CHARS, visible: ReadonlySet<string> = new Set()): string[] {
  const defined = new Set([...text.matchAll(DEFINES)].map((m) => m[1]!).filter((name) => name.length >= 4))
  // a fact still shown elsewhere after the compaction needs no naming here
  const order = [...new Set([...defined, ...(text.match(FACT) ?? []).filter(projectFact)])].filter((fact) => !visible.has(fact))
  const score = (fact: string) => (defined.has(fact) ? 2 : 0) + (known.has(fact) ? 1 : 0)
  const ranked = order.map((fact, i) => ({ fact, i, s: score(fact) })).sort((a, b) => b.s - a.s || a.i - b.i)
  const picked: string[] = []
  let used = 0
  for (const { fact } of ranked) {
    if (used + fact.length + 2 > budget) continue
    picked.push(fact)
    used += fact.length + 2
  }
  return picked
}
