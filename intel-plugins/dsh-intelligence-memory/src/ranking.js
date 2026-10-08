// Literal lexical ranking; no stored kind, fixture judgment, or model call is used.
export const DEFAULT_SYNONYM_GROUPS = [
  ['偏好', '喜欢', '喜爱', '喜好', '希望', '倾向'],
  ['职责', '责任', '负责', '管理'],
  ['回答', '答复', '回复'],
  ['审批', '批准', '许可', '授权'],
];
export const DEFAULT_RANKING_LIMITS = { maxCandidates: 128, maxCandidateBytes: 1048576 };
export const DEFAULT_RANKING_RATIOS = { minimumScoreRatio: .1, duplicatePatternPenalty: .1 };

const grammarWords = new Set('a an the my our your please for and or in of to with is are what which everything earlier previous review find conversation'.split(' '));
const preferenceVerbs = DEFAULT_SYNONYM_GROUPS[0];
export const DEFAULT_PREFERENCE_SUBJECTS = ['我', '用户'];
// Explicit subject aliases cover named statements without guessing who a name represents.
export function preferencePhrases(subjects) {
  if (!Array.isArray(subjects) || subjects.length < 1 || subjects.length > 16
    || subjects.some(subject => typeof subject !== 'string' || !/^[\p{Script=Han}a-zA-Z0-9 ]{1,32}$/u.test(subject) || !subject.trim())) {
    throw new RangeError('preferenceSubjects must contain 1–16 literal subject aliases of 1–32 characters');
  }
  return [...new Set(subjects)].flatMap(person =>
    ['', '更', '比较', '通常', '一直', '个人'].flatMap(modifier =>
      preferenceVerbs.flatMap(verb => [person + modifier + verb, person + ' ' + modifier + verb])));
}

export function personalPreferenceQuery(query) {
  if (/(?:偏好|喜好|希望)(?:设置|配置|字段|模板)/.test(query)) return false;
  return (/我|用户|个人/.test(query) && /偏好|喜好|喜爱|倾向|喜欢什么/.test(query))
    || (/我希望/.test(query) && /回答|答复|回复/.test(query)
      && /先给结论|解释理由|说明方式/.test(query));
}

export function validateSynonyms(groups) {
  if (!Array.isArray(groups) || groups.length > 16 || groups.some(group =>
    !Array.isArray(group) || group.length < 2 || group.length > 16
    || group.some(term => typeof term !== 'string' || !/^[\p{Script=Han}a-zA-Z0-9 ]{1,32}$/u.test(term) || !term.trim()))) {
    throw new RangeError('synonymGroups must contain at most 16 groups of 2–16 literal terms, each 1–32 characters');
  }
  return groups.map(group => [...new Set(group.map(term => term.toLowerCase()))]);
}

function hasPhrase(text, phrase) {
  if (/\p{Script=Han}/u.test(phrase)) return text.includes(phrase);
  return (' ' + text.replace(/[^a-z0-9]+/g, ' ') + ' ').includes(' ' + phrase + ' ');
}

export function queryEvidence(query, groups) {
  const text = query.toLowerCase();
  const concepts = groups.filter(group => group.some(term => hasPhrase(text, term)));
  const allWords = text.match(/[a-z0-9]{2,}/g) ?? [];
  const hasSubject = /\p{Script=Han}/u.test(text) || allWords.some(word => !grammarWords.has(word));
  const words = allWords.filter(word => !hasSubject || !grammarWords.has(word));
  return { text, concepts, words, personal: personalPreferenceQuery(query) };
}

function features(text) {
  const result = new Set(text.toLowerCase().match(/[a-z0-9]{2,}/g) ?? []);
  for (const run of text.match(/\p{Script=Han}+/gu) ?? []) {
    const chars = [...run];
    if (chars.length === 1) result.add(chars[0]);
    else for (let i = 0; i + 1 < chars.length; i++) result.add(chars[i] + chars[i + 1]);
  }
  return result;
}

export function rankCandidates(query, hits, evidence, ratios = DEFAULT_RANKING_RATIOS) {
  // Canonicalizing preference verbs avoids privileging the particular synonym
  // used in a broad preference question over other personal preferences.
  const normalized = evidence.personal ? query.replace(/偏好|喜欢|喜爱|喜好|希望|倾向/g, '偏好') : query;
  const queryFeatures = [...features(normalized)].filter(term =>
    !/^[a-z0-9]/.test(term) || evidence.words.includes(term));
  const conceptFeatures = evidence.concepts.map((group, index) => ({ group, key: `concept:${index}` }));
  const candidates = hits.map((hit, index) => {
    const found = features(hit.text), low = hit.text.toLowerCase();
    const present = queryFeatures.filter(term => found.has(term)
      || (/^\p{Script=Han}$/u.test(term) && hit.text.includes(term)));
    for (const { group, key } of conceptFeatures) if (group.some(term => hasPhrase(low, term))) present.push(key);
    return { hit, index, present: [...new Set(present)], pattern: [...new Set(present)].sort().join('|') };
  }).filter(candidate => candidate.present.length);
  // Repeated logs with identical query evidence count as one pattern, so their
  // volume cannot distort IDF or consume every result through score ties.
  const patterns = new Map(candidates.map(candidate => [candidate.pattern, candidate.present]));
  const frequencies = new Map();
  for (const present of patterns.values()) for (const term of present) frequencies.set(term, (frequencies.get(term) ?? 0) + 1);
  const idf = term => Math.log(1 + (patterns.size - frequencies.get(term) + .5) / (frequencies.get(term) + .5));
  for (const candidate of candidates) candidate.score = candidate.present.reduce((sum, term) => sum + idf(term), 0);
  const best = Math.max(0, ...candidates.map(candidate => candidate.score));
  const remaining = candidates.filter(candidate => candidate.score >= best * ratios.minimumScoreRatio);
  const chosen = [], counts = new Map();
  while (remaining.length) {
    const score = candidate => candidate.score - best * ratios.duplicatePatternPenalty * (counts.get(candidate.pattern) ?? 0);
    remaining.sort((a, b) => score(b) - score(a) || a.index - b.index);
    const next = remaining.shift();
    chosen.push(next.hit);
    counts.set(next.pattern, (counts.get(next.pattern) ?? 0) + 1);
  }
  return chosen;
}
