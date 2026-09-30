export class ReflectionLifetime {
  closed = false;
  #owned = new Set();
  begin(timeoutMs) {
    if (this.closed) return null;
    const controller = new AbortController();
    let finish;
    const done=new Promise(resolve=>{finish=resolve;});
    const entry = {controller, done, finish, timer: setTimeout(() => controller.abort('quiet reflection timeout'),timeoutMs)};
    this.#owned.add(entry);
    return entry;
  }
  async publish(entry, run) {
    entry.run=run;
    if (!this.closed) return true;
    await this.dispose(entry);
    return false;
  }
  dispose(entry) { return entry.disposal ??= Promise.resolve().then(()=>entry.run.dispose()); }
  release(entry) { clearTimeout(entry.timer); this.#owned.delete(entry); entry.finish(); }
  async close() {
    this.closed=true;
    const owned=[...this.#owned];
    for (const entry of this.#owned) {
      clearTimeout(entry.timer);entry.controller.abort('quiet unloaded');
      if(entry.run)this.dispose(entry).catch(()=>{});
    }
    await Promise.all(owned.map(entry=>entry.done));
  }
}
