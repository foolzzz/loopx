import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { delimiter, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = fileURLToPath(new URL("../", import.meta.url));
const versionProbe = "import json,sys; print(json.dumps({'executable':sys.executable,'version':list(sys.version_info[:2])}))";

function probe(candidate, root, env, prefix = []) {
  const pathCandidate = candidate.includes("/") || candidate.includes("\\");
  const command = pathCandidate ? resolve(root, candidate) : candidate;
  const result = spawnSync(command, [...prefix, "-c", versionProbe], {
    cwd: root, env, encoding: "utf8", timeout: 5_000,
  });
  if (result.status !== 0) return null;
  try {
    const { executable, version } = JSON.parse(result.stdout.trim());
    if (isAbsolute(executable) && Array.isArray(version)
      && (version[0] > 3 || (version[0] === 3 && version[1] >= 11))) {
      return pathCandidate ? command : executable;
    }
  } catch { /* A candidate that cannot report its runtime is not usable. */ }
  return null;
}

function windowsCandidates(root, env) {
  const candidates = [];
  const recorded = join(root, ".loopx-python");
  if (existsSync(recorded)) candidates.push([readFileSync(recorded, "utf8").split(/\r?\n/, 1)[0].trim()]);
  const versioned = [];
  for (const directory of (env.PATH ?? "").split(delimiter)) {
    try {
      for (const name of readdirSync(directory || ".")) {
        const match = /^python3\.(\d+)(?:\.exe)?$/i.exec(name);
        if (match) versioned.push({ minor: Number(match[1]), path: join(directory, name) });
      }
    } catch { /* Missing PATH entries are ordinary discovery misses. */ }
  }
  versioned.sort((a, b) => b.minor - a.minor);
  candidates.push(...versioned.map(({ path }) => [path]));
  candidates.push(["python3"], ["python"], ["py", "-3"]);
  return candidates;
}

/** Select the interpreter used by source-checkout tests; never install or mutate one. */
export function resolveTestPython({ env = process.env, repoRoot = repositoryRoot, platform = process.platform } = {}) {
  const root = resolve(repoRoot);
  for (const key of ["LOOPX_TEST_PYTHON", "LOOPX_PYTHON_BIN", "LOOPX_PYTHON"]) {
    if (!env[key]) continue;
    const selected = probe(env[key], root, env);
    if (selected) return selected;
    throw new Error(`${key} does not resolve to Python 3.11+; choose the test environment's Python executable.`);
  }
  const environmentPython = platform === "win32" ? join("Scripts", "python.exe") : join("bin", "python");
  for (const directory of [join(root, ".venv"), env.VIRTUAL_ENV]) {
    if (!directory) continue;
    const selected = probe(join(directory, environmentPython), root, env);
    if (selected) return selected;
  }
  if (platform !== "win32") {
    // Reuse the source launcher's .loopx-python/.venv/PATH precedence on POSIX.
    const selected = spawnSync("bash", [join(root, "scripts", "loopx-python.sh")], {
      cwd: root, env, encoding: "utf8", timeout: 10_000,
    });
    if (selected.status === 0) {
      const python = probe(selected.stdout.trim(), root, env);
      if (python) return python;
    }
  } else {
    for (const [candidate, ...prefix] of windowsCandidates(root, env)) {
      if (!candidate) continue;
      const selected = probe(candidate, root, env, prefix);
      if (selected) return selected;
    }
  }
  throw new Error("No Python 3.11+ interpreter found for LoopX tests. Run `uv sync --extra test` from this worktree or set LOOPX_TEST_PYTHON to a compatible executable.");
}
