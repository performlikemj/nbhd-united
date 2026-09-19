// OpenClaw 2026.9.1 hardcodes /tmp for lifecycle SQLite coordinator files,
// ignoring TMPDIR. Redirect just that installed module in this process so all
// writes stay inside the approved test home/worktree. Never edit shared runtime.
import { registerHooks } from 'node:module';
import { fileURLToPath } from 'node:url';

if (!process.env.TMPDIR?.startsWith('/Users/mjjones/worktrees/united-yuki-test/deploy/local-test/.state/')) {
  throw new Error('OpenClaw test launcher requires its isolated TMPDIR');
}
registerHooks({
  load(url, context, nextLoad) {
    const loaded = nextLoad(url, context);
    if (url.startsWith('file:') && /\/state-database-coordinator-[^/]+\.js$/.test(fileURLToPath(url))) {
      const source = String(loaded.source);
      const needle = ': "/tmp";';
      if (source.split(needle).length !== 2) {
        throw new Error('Installed OpenClaw lifecycle path changed; review the local path adapter');
      }
      return { ...loaded, source: source.replace(needle, ': process.env.TMPDIR;') };
    }
    return loaded;
  },
});
