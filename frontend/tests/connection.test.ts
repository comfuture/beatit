import { describe, expect, it } from "vitest";
import { actionErrorMessage, isTransientReadError } from "../app/utils/connection";

describe("temporary connection failures", () => {
  it("recognizes aborted reads, redeployment responses and network failures", () => {
    for (const error of [new DOMException("signal ended", "AbortError"), new DOMException("timed out", "TimeoutError"), new TypeError("Failed to fetch"), Object.assign(new Error("upstream failed"), { status: 503 })])
      expect(isTransientReadError(error)).toBe(true);
    expect(isTransientReadError(Object.assign(new Error("not found"), { status: 404 }))).toBe(false);
  });
  it("keeps transport details out of action errors while preserving useful validation", () => {
    expect(actionErrorMessage(new Error("signal ended"))).toBe("서버에 연결할 수 없습니다. 다시 시도하세요.");
    expect(actionErrorMessage(new Error("템포를 확인하세요."))).toBe("템포를 확인하세요.");
    expect(actionErrorMessage(Object.assign(new Error("대기 작업은 3개까지 가능합니다."), { status: 429 }))).toBe("대기 작업은 3개까지 가능합니다.");
  });
});
