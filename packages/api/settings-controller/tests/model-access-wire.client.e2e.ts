/** Built Client wire acceptance; requires the generated Remote Typert artifact. */
import { expect, it } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import type { ConnectionHandle } from '@deepseek-ai/dsh-client-connection/client'
import TypertRegistry from '../../../typert/registry/src/index.ts'
import * as ClientGateway from '../../gateway/src/client/index.ts'
import modelAccessRemote from '../lib/typert.remote-client.js'

it('mounts the complete generated model access contribution in the real Client gateway', async () => {
  const client = new Context()
  try {
    await client.plugin(TypertRegistry)
    const connection: ConnectionHandle = {
      isLoopback: true,
      generation: { getSnapshot: () => undefined, subscribe: () => () => {} },
      state: { getSnapshot: () => undefined, subscribe: () => () => {} },
      reconnect: () => {},
      rpc: { call: async () => ({ ok: true, value: null }), open: async function *() {} },
      registerGenerationSource: () => () => {},
      start: () => ({ stop: () => {} }),
    }
    client.reflect.provide('connection', connection)
    await client.plugin(ClientGateway)
    const dispose = await client.remote.$mount(modelAccessRemote)
    expect(client.get('remote.modelAccess')).toBeDefined()
    await dispose()
    expect(client.get('remote.modelAccess')).toBeUndefined()
  } finally {
    await client.fiber.dispose()
  }
})
