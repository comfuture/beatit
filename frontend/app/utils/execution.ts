import type { SystemInfo } from "~/types/studio";

export const isColabExecution = (system: SystemInfo | null) =>
  system?.execution_backend === "colab";

export function colabConnectionUnavailable(system: SystemInfo | null): boolean {
  const state = system?.remote_runtime?.state;
  return isColabExecution(system) && (!state || state === "unavailable" || state === "error");
}

export function runtimeStatus(system: SystemInfo | null): string {
  if (!isColabExecution(system)) return "";
  const runtime = system?.remote_runtime;
  if (!runtime) return "Colab GPU 상태 확인 중";
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
      return runtime.prepared ? "준비된 GPU 재사용 가능" : "Colab 실행 환경 준비 대기";
  }
}
