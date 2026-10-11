// Run with the existing source runtime: tsx --tsconfig tsconfig.base.json --test this-file.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Context } from '@deepseek-ai/cordis';
import { ToolCallId } from '@deepseek-ai/dsh-llm';
import { Session, SessionId } from '@deepseek-ai/dsh-session';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import Tools, { defineContentToolFixture } from '@deepseek-ai/dsh-tools';
import Approval from '@deepseek-ai/dsh-user-approval';
import * as IntelGate from '../index.js';

for (const policy of ['ask', 'never']) {
  for (const [name, args, level] of [
    ['read', {}, 'READ'], ['write', {}, 'WRITE'],
    ['bash', { command: 'echo T11' }, 'EXEC'], ['bash', { command: 'rm -rf /' }, 'BLOCKED'],
  ]) {
    test(`${policy}: ${level} uses the same input and cannot bypass a denial`, async () => {
      const ctx = new Context();
      try {
        await ctx.plugin(SystemPrompt);await ctx.plugin(Tools);
        await ctx.plugin(Approval, { policy, waitTimeoutMs: 25 });await ctx.plugin(IntelGate, {});
        const session = Session.create(SessionId(`t11-${policy}-${level}`));
        session.append('turn/start', { turn: 1 });const agent = { session };
        let runs = 0, answers = 0;
        ctx.tools.register(defineContentToolFixture({
          name, description: 'Counted fixture; no file or command executes',
          parameters: { command: { type: 'string' } },
          async execute() { runs += 1; return [{ type: 'text', text: 'executed fixture' }]; },
        }));
        ctx.on('approval/request', () => { answers += 1; return Promise.resolve('allowed-once'); });
        const result = await ctx.tools.execute({
          callId: ToolCallId('t11-gate'), name, arguments: args, agent, signal: new AbortController().signal,
        });
        const allowed = level === 'READ' || policy === 'ask' && level !== 'BLOCKED';
        assert.equal(runs, allowed ? 1 : 0);assert.equal(result.isError === true, !allowed);
        assert.equal(answers, policy === 'ask' && (level === 'WRITE' || level === 'EXEC') ? 1 : 0);
        const decided = session.ownEvents().filter(event => event.type === 'approval/decided');
        assert.equal(decided.length, level === 'WRITE' || level === 'EXEC' ? 1 : 0);
        if (decided.length) assert.equal(decided[0].data.outcome, policy === 'ask' ? 'allowed-once' : 'rejected');
        if (level === 'BLOCKED') assert.equal(result.error?.info.code, 'APPROVAL_BLOCKED');
      } finally { await ctx.fiber.dispose(); }
    });
  }
}
