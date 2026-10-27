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
const serverColab = (
  state: NonNullable<SystemInfo["remote_runtime"]>["state"],
  overrides: Partial<NonNullable<SystemInfo["remote_runtime"]>> = {},
): SystemInfo => ({
  ...colab(state),
  remote_runtime: {
    ...colab(state).remote_runtime!,
    allocation_mode: "server", can_allocate: true, manager_error: null,
    ...overrides,
  },
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

  it("allows on-demand server allocation without calling an idle GPU ready", () => {
    const idle = serverColab("idle");
    expect(colabConnectionUnavailable(idle)).toBe(false);
    expect(runtimeStatus(idle)).toBe("분석 시 Colab GPU 자동 할당");
    expect(runtimeStatus(serverColab("allocating"))).toBe("Colab GPU 할당 중");
    expect(runtimeStatus(serverColab("ready"))).toBe("Colab 분석 환경 확인 중");
    expect(runtimeStatus(serverColab("ready", { prepared: true }))).toBe("준비된 GPU 재사용 가능");
  });

  it("blocks server allocation when its manager is offline, even with a prepared GPU", () => {
    for (const state of ["idle", "ready", "preparing", "allocating"] as const) {
      const offline = serverColab(state, { can_allocate: false, prepared: true });
      expect(colabConnectionUnavailable(offline)).toBe(true);
      expect(runtimeStatus(offline)).toBe("Colab 관리자 연결 대기");
    }
    expect(colabConnectionUnavailable(serverColab("idle", { can_allocate: undefined }))).toBe(true);
    expect(colabConnectionUnavailable(serverColab("unavailable"))).toBe(true);
    expect(colabConnectionUnavailable(serverColab("error"))).toBe(true);
  });

  it("shows safe manager errors and blocks new work even if allocation was previously available", () => {
    const error = "서버 Colab 인증을 다시 진행해야 합니다.";
    const failed = serverColab("idle", { manager_error: error });
    expect(colabConnectionUnavailable(failed)).toBe(true);
    expect(runtimeStatus(failed)).toBe(error);
  });

  it("keeps operator readiness independent of server manager fields", () => {
    const manual = serverColab("ready", { allocation_mode: "operator", can_allocate: false, prepared: true });
    expect(colabConnectionUnavailable(manual)).toBe(false);
    expect(runtimeStatus(manual)).toBe("준비된 GPU 재사용 가능");
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
