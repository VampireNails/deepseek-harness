import { existsSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { FeedStore } from '../../intel-plugins/dsh-intelligence-feed/src/feed.js';
import { ArtifactStore } from '../../intel-plugins/dsh-intelligence-artifacts/src/artifacts.js';
import { memoryReferences } from '../../intel-plugins/shared/publication.js';

/** Inspect readable, persisted outputs; a broken or absent memory reference cannot satisfy a run. */
export function inspectVisibleOutputs(home) {
  const outputs=[];
  const feedDir=join(home,'intel-feed'),artifactDir=join(home,'intel-artifacts');
  if(existsSync(feedDir)) outputs.push(...new FeedStore(feedDir).listPosts(100));
  if(existsSync(artifactDir)){
    const store=new ArtifactStore(artifactDir);
    for(const id of readdirSync(artifactDir)){
      const latest=store.get(id);
      if(!latest)continue;
      // Older versions can belong to this run even after a later task updates the same title.
      for(const version of latest.versions??[]) {
        const output=store.get(id,version.v);
        if(output)outputs.push(output);
      }
    }
  }
  return outputs.filter(output=>{
    if(!output.idempotencyKey)return false;
    try { memoryReferences(output.references, undefined, home, output.memoryDirectory ?? join(home,'intel-memory')); return true; } catch { return false; }
  });
}
