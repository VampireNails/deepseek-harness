// Equity-analysis instructions are sourced from workflow.md so Web and headless
// consume exactly one editable workflow contract.
import fs from 'node:fs';
import path from 'node:path';

const workflowPath = path.join(process.cwd(), 'apps', 'cli', 'config', 'agent-presets', 'equity-analysis', 'workflow.md');
const workflow = fs.readFileSync(workflowPath, 'utf8').trim();

export const instructions = workflow;
export default instructions;
