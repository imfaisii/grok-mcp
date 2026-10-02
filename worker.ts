import { Container, getContainer } from "@cloudflare/containers";

// Secrets come from `wrangler secret put`; the rest are `vars` in
// wrangler.jsonc. Either way they reach the Python server as plain env vars.
const PASSTHROUGH = [
  "XAI_API_KEY",
  "MCP_AUTH_TOKEN",
  "MCP_URL_SECRET",
  "MCP_OAUTH_SECRET",
  "MCP_OAUTH_PASSWORD",
  "R2_ACCOUNT_ID",
  "R2_ACCESS_KEY_ID",
  "R2_SECRET_ACCESS_KEY",
  "R2_BUCKET",
  "R2_PUBLIC_BASE",
  "R2_CHATS_BUCKET",
  "TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID",
  "DOMAIN",
  "MCP_PATH",
] as const;

type Env = Cloudflare.Env & Partial<Record<(typeof PASSTHROUGH)[number], string>>;

// The whole MCP server (Python, ffmpeg, rembg) runs in this container. One
// instance serves everything: the server keeps no per-request state worth
// sharding, and video jobs are single long requests rather than sessions.
export class GrokMcp extends Container<Env> {
  defaultPort = 8000;
  // Outlives the longest single call (generate_video polls for up to ~10 min);
  // fetches renew the timer, so an idle instance still scales to zero.
  sleepAfter = "20m";
  envVars: Record<string, string>;

  constructor(...args: ConstructorParameters<typeof Container<Env>>) {
    super(...args);
    const env = args[1];
    this.envVars = { MCP_TRANSPORT: "http", MCP_HOST: "0.0.0.0", MCP_PORT: "8000" };
    for (const key of PASSTHROUGH) {
      const value = env[key];
      if (value) this.envVars[key] = value;
    }
  }
}

export default {
  fetch(request: Request, env: Env): Promise<Response> {
    return getContainer(env.GROK_MCP).fetch(request);
  },
} satisfies ExportedHandler<Env>;
