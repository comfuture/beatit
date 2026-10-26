import { describe, expect, it } from "vitest";
import type { SystemInfo } from "../app/types/studio";
import { colabConnectionUnavailable, isColabExecution, runtimeStatus } from "../app/utils/execution";

const system: SystemInfo = {
  devices: ["cuda"], default_device: "cuda", ffmpeg: true, musescore: false,
  verovio: true, max_upload_bytes: 1024 ** 3, max_duration_seconds: 1800,
};
const colab = (state: NonNullable<SystemInfo["remote_runtime"]>["state"], prepared = false): SystemInfo => ({
  ...system,
  execution_backend: "colab",
  remote_runtime: { state, prepared, accelerator: "T4", idle_timeout_seconds: 600 },
});

describe("execution location and runtime readiness", () => {
  it("keeps existing local system responses compatible", () => {
    expect(isColabExecution(null)).toBe(false);
    expect(isColabExecution(system)).toBe(false);
    expect(runtimeStatus(system)).toBe("");
    expect(colabConnectionUnavailable(system)).toBe(false);
  });

  it("blocks new analysis until an operator runtime is attached", () => {
    expect(colabConnectionUnavailable({ ...system, execution_backend: "colab" })).toBe(true);
    expect(colabConnectionUnavailable(colab("unavailable"))).toBe(true);
    expect(colabConnectionUnavailable(colab("error"))).toBe(true);
    expect(colabConnectionUnavailable(colab("ready", true))).toBe(false);
    expect(colabConnectionUnavailable(colab("preparing"))).toBe(false);
  });

  it("does not claim a GPU is ready before preparation is confirmed", () => {
    expect(runtimeStatus({ ...system, execution_backend: "colab" })).toBe("Colab GPU 상태 확인 중");
    expect(runtimeStatus(colab("idle"))).toBe("Colab 실행 환경 준비 대기");
    expect(runtimeStatus(colab("ready"))).toBe("Colab 분석 환경 확인 중");
    expect(runtimeStatus(colab("ready", true))).toBe("준비된 GPU 재사용 가능");
  });

  it.each([
    ["allocating", "Colab GPU 할당 중"],
    ["preparing", "Colab 분석 환경 준비 중"],
    ["running", "Colab GPU 분석 중"],
    ["releasing", "Colab GPU 정리 중"],
    ["unavailable", "Colab 연결 대기"],
    ["error", "Colab GPU 연결 확인 필요"],
  ] as const)("explains the %s state", (state, expected) => {
    expect(runtimeStatus(colab(state))).toBe(expected);
  });
});
