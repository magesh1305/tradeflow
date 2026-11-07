// Remembers (per browser) whether the Start here guide was opened, so it shows
// automatically only the first time. A convenience only: safe to lose.

const KEY = "tradeflow.start-guide-seen";

export function startGuideSeen(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    return true; // storage blocked: don't keep redirecting
  }
}

export function markStartGuideSeen(): void {
  try {
    localStorage.setItem(KEY, "1");
  } catch {
    /* ignore */
  }
}
