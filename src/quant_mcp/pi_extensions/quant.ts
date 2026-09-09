/** Repo-aware Pi tools. Domain functions/classes remain ordinary Python code. */
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { spawn } from "node:child_process";
import { readFile, writeFile, realpath } from "node:fs/promises";
import path from "node:path";

export default function (pi: ExtensionAPI) {
  const workspace = process.env.QUANT_PI_WORKSPACE!;
  const python = process.env.QUANT_PI_PYTHON!;
  const timeout = process.env.QUANT_PI_EXECUTION_TIMEOUT || "60";
  let running = false;
  let executions = 0;

  pi.on("session_start", async () => {
    pi.setActiveTools(["read", "grep", "find", "ls", "write", "edit", "run_python"]);
  });
  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName === "write" || event.toolName === "edit") {
      const target = path.resolve(ctx.cwd, String(event.input.path));
      // Resolve the parent to reject symlink escapes as well as ../ paths.
      let parent: string;
      try { parent = await realpath(path.dirname(target)); }
      catch { return { block: true, reason: "Write scripts directly in the task workspace." }; }
      let resolved = path.join(parent, path.basename(target));
      try { resolved = await realpath(target); } catch {}
      if (!resolved.startsWith(workspace + path.sep)) {
        return { block: true, reason: "Library source and demos are read-only. Write task code in WORKSPACE." };
      }
    }
    if (event.toolName === "read") {
      const name = path.basename(String(event.input.path));
      if (name === ".env" || (name.startsWith(".env.") && name !== ".env.example")) {
        return { block: true, reason: "Credentials are configured by the host; do not read .env." };
      }
    }
  });
  pi.registerTool({
    name: "run_python",
    label: "Run Python task",
    description: "Execute and validate Python using existing repository libraries. Supply complete code OR the path of a task script. Saves analysis.py, records actual function/method calls, and returns output or errors for repair. Fresh Python process per run. No human review. A task is complete only when this tool returns status success.",
    parameters: Type.Object({
      code: Type.Optional(Type.String({ description: "Complete Python script to execute." })),
      script_path: Type.Optional(Type.String({ description: "Existing script inside WORKSPACE; use instead of code." })),
    }),
    async execute(_id, args, signal) {
      if (++executions > Number(process.env.QUANT_PI_MAX_EXECUTIONS || "8")) throw new Error("Python execution budget exhausted.");
      if (running) throw new Error("Run one Python script at a time; wait for its result before repairing.");
      if ((args.code === undefined) === (args.script_path === undefined)) throw new Error("Supply exactly one of code or script_path.");
      running = true;
      try {
        let code = args.code;
        if (args.script_path !== undefined) {
          const filename = await realpath(path.resolve(workspace, args.script_path));
          if (!filename.startsWith(workspace + path.sep)) throw new Error("Script must be inside WORKSPACE.");
          code = await readFile(filename, "utf8");
        }
        await writeFile(path.join(workspace, "analysis.py"), code! + "\n");
        const output = await new Promise<string>((resolve, reject) => {
          // Inherit the MCP worker's process group so its deadline kills all descendants.
          const child = spawn(python, ["-m", "quant_mcp.pi_execution", workspace, timeout], {
            cwd: workspace, env: process.env, stdio: ["ignore", "pipe", "pipe"],
          });
          let stdout = "", stderr = "";
          const abort = () => child.kill("SIGTERM");
          signal?.addEventListener("abort", abort, { once: true });
          if (signal?.aborted) abort();
          child.stdout.on("data", d => { stdout += d; });
          child.stderr.on("data", d => { stderr = (stderr + d).slice(-12000); });
          child.on("error", reject);
          child.on("close", code => {
            signal?.removeEventListener("abort", abort);
            if (code !== 0) reject(new Error(`Executor exited ${code}: ${stderr}`));
            else resolve(stdout);
          });
        });
        const response = JSON.parse(output);
        return { content: [{ type: "text", text: JSON.stringify(response) }], details: response };
      } finally { running = false; }
    },
  });
}
