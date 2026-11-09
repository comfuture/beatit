const sessionId = /^[0-9a-f]{32}$/;
const sessionRoute = /^\/sessions\/([0-9a-f]{32})$/;

/** Undefined is an invalid route; null is the explicitly blank workspace. */
export function sessionIdFromPath(path: string): string | null | undefined {
  if (path === "/") return null;
  return sessionRoute.exec(path)?.[1];
}

export function sessionPath(id: string): string {
  if (!sessionId.test(id)) throw new Error("올바르지 않은 세션 주소입니다.");
  return `/sessions/${id}`;
}

export function googleLoginPath(path: string): string {
  const id = sessionIdFromPath(path);
  return id
    ? `/api/auth/google?return_to=${encodeURIComponent(sessionPath(id))}`
    : "/api/auth/google";
}
