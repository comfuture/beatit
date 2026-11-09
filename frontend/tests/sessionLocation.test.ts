import { describe, expect, it } from "vitest";
import { googleLoginPath, sessionIdFromPath, sessionPath } from "../app/utils/sessionLocation";

const id = "a".repeat(32);
describe("canonical private session locations", () => {
  it("distinguishes an explicit blank workspace from a session permalink", () => {
    expect(sessionIdFromPath("/")).toBeNull();
    expect(sessionPath(id)).toBe(`/sessions/${id}`);
    expect(sessionIdFromPath(`/sessions/${id}`)).toBe(id);
  });
  it("rejects other paths and malformed identifiers", () => {
    for (const path of ["/sessions/short", `/sessions/${id.toUpperCase()}`, `/sessions/${id}/files`, "//evil.example", "/other"]) {
      expect(sessionIdFromPath(path)).toBeUndefined();
      expect(googleLoginPath(path)).toBe("/api/auth/google");
    }
    expect(() => sessionPath("../private")).toThrow();
  });
  it("requests only an allowlisted local return path through Google login", () => {
    expect(googleLoginPath("/")).toBe("/api/auth/google");
    expect(googleLoginPath(`/sessions/${id}`)).toBe(`/api/auth/google?return_to=%2Fsessions%2F${id}`);
  });
});
