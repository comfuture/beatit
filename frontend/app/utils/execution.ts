import type { SystemInfo } from "~/types/studio";

export const isColabExecution = (system: SystemInfo | null) =>
  system?.execution_backend === "colab";

export function colabConnectionUnavailable(system: SystemInfo | null): boolean {
  if (!isColabExecution(system)) return false;
  const runtime = system?.remote_runtime;
  if (!runtime || runtime.state === "unavailable" || runtime.state === "error") return true;
  return runtime.allocation_mode === "server" &&
    (runtime.can_allocate !== true || !!runtime.manager_error);
}

export function runtimeStatus(system: SystemInfo | null): string {
  if (!isColabExecution(system)) return "";
  const runtime = system?.remote_runtime;
  if (!runtime) return "Colab GPU 상태 확인 중";
  if (runtime.allocation_mode === "server") {
    if (runtime.manager_error) return runtime.manager_error;
    if (runtime.can_allocate !== true) return "Colab 관리자 연결 대기";
  }
  switch (runtime.state) {
    case "allocating":
      return "Colab GPU 할당 중";
    case "preparing":
      return "Colab 분석 환경 준비 중";
    case "ready":
      return runtime.prepared ? "준비된 GPU 재사용 가능" : "Colab 분석 환경 확인 중";
    case "running":
      return "Colab GPU 분석 중";
    case "releasing":
      return "Colab GPU 정리 중";
    case "unavailable":
      return "Colab 연결 대기";
    case "error":
      return "Colab GPU 연결 확인 필요";
    case "idle":
      if (runtime.prepared) return "준비된 GPU 재사용 가능";
      return runtime.allocation_mode === "server"
        ? "분석 시 Colab GPU 자동 할당"
        : "Colab 실행 환경 준비 대기";
  }
}
