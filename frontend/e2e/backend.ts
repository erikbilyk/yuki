import { spawn } from "node:child_process";
import type { ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, openSync, readFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";

const HOST = "127.0.0.1";
const BACKEND_DIR = path.resolve(import.meta.dirname, "..", "..", "backend");
const ENV_EXAMPLE = path.resolve(import.meta.dirname, "..", "..", ".env.example");
const RESTART_DELAY_MS = 500;
const HEALTH_TIMEOUT_MS = 60_000;

function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, HOST, () => {
      const { port } = server.address() as net.AddressInfo;
      server.close(() => resolve(port));
    });
  });
}

/** Every setting name in `.env.example`, so that each one is set and the working copy's `.env` never applies. */
function settingNames(): string[] {
  return readFileSync(ENV_EXAMPLE, "utf8")
    .split("\n")
    .map((line) => line.split("=")[0].trim())
    .filter((name) => name.startsWith("WG_"));
}

function healthy(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const request = http.get({ host: HOST, port, path: "/healthz", timeout: 1000 }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("error", () => resolve(false));
    request.on("timeout", () => {
      request.destroy();
      resolve(false);
    });
  });
}

/**
 * The backend as a real process on its own port and database, in demo mode with the offline model.
 * Like `scripts/supervise.sh`, it starts the process again after any exit that was not asked for,
 * so the crash button ends and restores the service.
 */
export class Backend {
  port = 0;
  starts = 0;
  private child: ChildProcess | null = null;
  private stopping = false;
  private readonly env: Record<string, string>;
  private readonly logFd: number;

  constructor(workdir: string) {
    mkdirSync(workdir, { recursive: true });
    this.logFd = openSync(path.join(workdir, "server.log"), "a");
    const inherited = Object.fromEntries(
      Object.entries(process.env).filter(([name, value]) => !name.startsWith("WG_") && value !== undefined),
    ) as Record<string, string>;
    const settings: Record<string, string> = Object.fromEntries(settingNames().map((name) => [name, ""]));
    Object.assign(settings, {
      WG_DB_PATH: path.join(workdir, "wg.db"),
      WG_FAULT_WAIT_FILE: path.join(workdir, "fault.wait"),
      WG_DEMO_MODE: "1",
      WG_LLM_PROVIDER: "stub",
      WG_CODE_HMAC_KEY: randomBytes(32).toString("hex"),
      WG_SESSION_SECRET: randomBytes(32).toString("hex"),
      WG_CHANNEL_SECRET: randomBytes(32).toString("hex"),
    });
    this.env = { ...inherited, ...settings, PYTHONUNBUFFERED: "1" };
  }

  get url(): string {
    return `http://${HOST}:${this.port}`;
  }

  async start(): Promise<void> {
    this.port = await freePort();
    this.env["WG_PORT"] = String(this.port);
    this.launch();
    await this.waitHealthy();
  }

  private launch(): void {
    this.starts += 1;
    const child = spawn("uv", ["run", "--no-sync", "python", "-m", "wg.api.serve"], {
      cwd: BACKEND_DIR,
      env: this.env,
      stdio: ["ignore", this.logFd, this.logFd],
    });
    this.child = child;
    child.on("exit", () => {
      if (this.child === child && !this.stopping) {
        setTimeout(() => {
          if (!this.stopping) this.launch();
        }, RESTART_DELAY_MS);
      }
    });
  }

  async waitHealthy(timeoutMs = HEALTH_TIMEOUT_MS): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (!(await healthy(this.port))) {
      if (Date.now() > deadline) throw new Error(`backend not healthy after ${timeoutMs} ms`);
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
  }

  async stop(): Promise<void> {
    this.stopping = true;
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    await new Promise<void>((resolve) => {
      child.once("exit", () => resolve());
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 10_000);
    });
  }
}
