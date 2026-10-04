import { access, copyFile, lstat, mkdir, readFile, realpath, symlink, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// Add observability bundles without replacing profile patches, model settings or credentials.
const [profilesRoot, backupRoot] = process.argv.slice(2).map(value => resolve(value));
if (!profilesRoot || !backupRoot) throw new Error("Pass profiles root and a new backup directory");
const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const plugins = {
  web: ["dsh-intelligence-sysevents", "dsh-intelligence-tokenlog"],
  evolve: ["dsh-intelligence-tokenlog"],
};
const plan = [];
for (const [profile, names] of Object.entries(plugins)) {
  const directory = join(profilesRoot, profile);
  const file = join(directory, "package.json");
  const json = JSON.parse(await readFile(file, "utf8"));
  if (!Array.isArray(json.dsh?.profile?.bundles)) throw new Error("Missing profile bundles: " + profile);
  const links = [];
  for (const name of names) {
    const target = join(sourceRoot, "intel-plugins", name);
    await access(join(target, "package.json"));
    const link = join(directory, "node_modules", name);
    let exists = false;
    try {
      await lstat(link);
      exists = true;
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    if (exists && await realpath(link) !== await realpath(target)) throw new Error("Conflicting plugin link: " + name);
    links.push({ link, target, created: !exists });
    json.dependencies ??= {};
    json.dependencies[name] = "link:" + target;
    if (!json.dsh.profile.bundles.includes(name)) json.dsh.profile.bundles.push(name);
  }
  plan.push({ profile, file, json, links });
}
await mkdir(backupRoot, { mode: 0o700 });
for (const item of plan) {
  await copyFile(item.file, join(backupRoot, item.profile + ".package.json"), constants.COPYFILE_EXCL);
}
await writeFile(join(backupRoot, "links.json"), JSON.stringify(plan.flatMap(item => item.links), null, 2) + "\n", { mode: 0o600 });
for (const item of plan) {
  await mkdir(join(dirname(item.file), "node_modules"), { recursive: true });
  for (const { link, target, created } of item.links) if (created) await symlink(target, link);
  await writeFile(item.file, JSON.stringify(item.json, null, 2) + "\n");
}
console.log(JSON.stringify({ profiles: Object.keys(plugins), backupRoot, plugins }));
