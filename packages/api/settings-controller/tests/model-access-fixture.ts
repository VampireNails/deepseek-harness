import LlmRuntime from '@deepseek-ai/dsh-llm'
import AuthorizationService from '@deepseek-ai/dsh-authorization'
import * as PiAi from '@deepseek-ai/dsh-llm-pi-ai'
import { configurationFixture } from '../../../settings/settings/tests/configuration-fixture.ts'
import { MemoryCredentials } from '../../../credentials/credentials/tests/memory.ts'
import SettingsController from '../src/index.ts'
import type { ModelAccessOptions } from '../src/model-access.ts'


export async function boot(options: ModelAccessOptions = {}) {
  const fixture = await configurationFixture({ schema: PiAi.Config, setup(ctx) { new LlmRuntime(ctx) }, apply(ctx, config) {
    if (ctx.fiber.entry?.options.id === 'first') PiAi.apply(ctx, config as PiAi.Config)
  } })
  const { ctx } = fixture
  await ctx.plugin(MemoryCredentials)
  await ctx.plugin(AuthorizationService)
  await ctx.plugin(SettingsController, { modelAccess: options })
  await ctx.fiber.await()
  return { ...fixture, controller: ctx.modelAccessController }
}
