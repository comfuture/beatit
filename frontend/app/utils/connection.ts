/** Read failures during a deployment must not erase an already loaded editor. */
export function isTransientReadError(reason: unknown): boolean {
  const error = reason as { status?: number; name?: string; message?: string } | null;
  return !!error && (
    (typeof error.status === "number" && (error.status >= 500 || error.status === 408 || error.status === 429)) ||
    ["AbortError", "TimeoutError", "SyntaxError"].includes(error.name || "") ||
    /signal ended|failed to fetch|fetch failed|network|load failed|connection (?:closed|reset)/i.test(error.message || "")
  );
}

export function actionErrorMessage(reason: unknown): string {
  const status = (reason as { status?: number } | null)?.status;
  if (isTransientReadError(reason) && status !== 429)
    return "서버에 연결할 수 없습니다. 다시 시도하세요.";
  return reason instanceof Error ? reason.message : "요청을 완료하지 못했습니다. 다시 시도하세요.";
}
