// Macro-analysis instructions are sourced from workflow.md so Web and headless
// consume exactly one editable workflow contract.
//
// Preset root moved (v0.1.2-alpha.3): apps/cli/config/agent-presets ->
// packages/preset/agent-presets/presets. The old absolute-via-cwd path no longer
// resolves. Resolve workflow.md relative to THIS file first (works for both the
// repo copy and the ~/.dsh deployment copy), then fall back to ~/.dsh.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const home = process.env.USERPROFILE || process.env.HOME || '';
const candidates = [
  path.join(here, 'workflow.md'),
  path.join(home, '.dsh', '.agent-presets', 'macro-analysis', 'workflow.md'),
];

let workflow = null;
for (const candidate of candidates) {
  try {
    workflow = fs.readFileSync(candidate, 'utf8').trim();
    break;
  } catch {}
}
if (workflow === null) {
  throw new Error(`preset macro-analysis: workflow.md not found; tried ${candidates.join(' | ')}`);
}

export const instructions = workflow;
export default instructions;
