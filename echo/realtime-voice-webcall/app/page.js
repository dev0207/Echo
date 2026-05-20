import { readFile } from "node:fs/promises";
import path from "node:path";

import WebcallExperience from "../components/webcall-experience";

async function loadRealtimeDispatcherPrompt() {
  const promptPath = path.join(
    process.cwd(),
    "prompts",
    "realtime_dispatcher_system.txt",
  );

  try {
    const content = await readFile(promptPath, "utf-8");
    return content.trim();
  } catch {
    return "";
  }
}

export default async function Page() {
  const realtimeDispatcherPrompt = await loadRealtimeDispatcherPrompt();
  return <WebcallExperience realtimeDispatcherPrompt={realtimeDispatcherPrompt} />;
}
