import { Honcho } from "@honcho-ai/sdk";

export interface HonchoConfig {
  apiKey: string;
  userName: string;
  assistantName: string;
  baseUrl: string;
  workspaceId: string;
}

/**
 * Read configuration from environment variables.
 * No longer requires a Request — all config comes from env vars.
 */
export function parseConfig(): HonchoConfig {
  const workspaceId = process.env.MCP_WORKSPACE_ID || "default";
  const baseUrl = process.env.HONCHO_BASE_URL || "http://localhost:8000";
  const apiKey = process.env.HONCHO_API_KEY || "";
  const userName = process.env.MCP_USER_NAME || "default";
  const assistantName = process.env.MCP_ASSISTANT_NAME || "Assistant";

  return {
    apiKey,
    userName,
    assistantName,
    baseUrl,
    workspaceId,
  };
}

export function createClient(config: HonchoConfig): Honcho {
  return new Honcho({
    apiKey: config.apiKey || "dummy",
    baseURL: config.baseUrl,
    workspaceId: config.workspaceId,
  });
}
