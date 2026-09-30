// In-process capability: labels, prompts and persisted session metadata cannot
// mint this grant. Only the run returned to Quiet carries the exact Agent.
export class QuietMemoryAuthority {
  #grants = new WeakMap();
  #pending = new Map();
  #closed = false;

  begin(signal, parentSession) {
    if (this.#closed || signal.aborted) return () => {};
    let resolve, settled = false;
    const ready = new Promise(done => { resolve = done; });
    const finish = run => {
      if (settled) return;
      settled = true;
      signal.removeEventListener('abort', aborted);
      if (!this.#closed && !signal.aborted && run?.localAgent)
        this.#grants.set(run.localAgent, {signal, remaining: 2});
      this.#pending.delete(ready);
      resolve();
    };
    const aborted = () => finish();
    this.#pending.set(ready, {finish, parentSession});
    signal.addEventListener('abort', aborted, {once: true});
    return finish;
  }

  async authorize(exec) {
    if (this.#closed || exec.name !== 'memory_write' || !exec.agent || exec.signal?.aborted) return false;
    // Only an unpublished child of an actively reflecting parent needs the
    // identity barrier. Existing grants and ordinary calls never wait for it.
    const parentSession=exec.agent.session?.header?.parentSession;
    while (!this.#grants.has(exec.agent) && parentSession && !this.#closed && !exec.signal?.aborted) {
      const relevant=[...this.#pending].filter(([,entry])=>entry.parentSession===parentSession).map(([ready])=>ready);
      if (!relevant.length) break;
      await new Promise(done => {
        const finish=()=>{exec.signal?.removeEventListener('abort',finish);done();};
        exec.signal?.addEventListener('abort',finish,{once:true});
        Promise.race(relevant).then(finish);
      });
    }
    const grant = this.#grants.get(exec.agent);
    if (this.#closed || !grant || grant.signal.aborted || exec.signal?.aborted || !grant.remaining) return false;
    grant.remaining--;
    return true;
  }

  release(agent) { if (agent) this.#grants.delete(agent); }
  close() {
    this.#closed = true;
    for (const {finish} of [...this.#pending.values()]) finish();
  }
}
