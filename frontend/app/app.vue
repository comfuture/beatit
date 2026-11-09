<script setup lang="ts">
import type { SignedInUser } from "~/types/auth";

const user = shallowRef<SignedInUser | null>(null);
const checking = ref(true);
const signingOut = ref(false);
const authError = ref("");
type LogoutNotice = {
  ownerId: string;
  nonce: string;
  phase: "pending" | "completed" | "error";
};
const blockedLogout = shallowRef<LogoutNotice | null>(null);
const logoutPending = computed(() => signingOut.value || blockedLogout.value?.phase === "pending");
const logoutFailure = "로그아웃을 완료하지 못했습니다. 다시 시도하세요.";
let disposed = false;
let revision = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let pending: AbortController | undefined;

function clearWorkspace() {
  user.value = null;
  // Legacy selection has no owner and must never select an account's project.
  localStorage.removeItem("beatit-session");
}
async function checkSession() {
  if (disposed || signingOut.value || blockedLogout.value) return;
  clearTimeout(timer);
  pending?.abort();
  const request = new AbortController();
  pending = request;
  const token = ++revision;
  try {
    const response = await fetch("/api/auth/me", {
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(10000)]),
    });
    if (!response.ok) throw new Error("로그인 상태를 확인할 수 없습니다. 잠시 후 다시 시도하세요.");
    const data: { user: SignedInUser | null } = await response.json();
    if (disposed || request.signal.aborted || token !== revision) return;
    if (data.user && (typeof data.user.id !== "string" || !data.user.id))
      throw new Error("로그인 응답을 확인할 수 없습니다.");
    // The keyed workspace unmounts on account changes, releasing editor and media.
    user.value = data.user;
    authError.value = "";
  } catch (reason) {
    if (disposed || request.signal.aborted || token !== revision) return;
    clearWorkspace();
    authError.value = reason instanceof Error ? reason.message : String(reason);
  } finally {
    if (!disposed && token === revision && !signingOut.value) {
      checking.value = false;
      timer = setTimeout(checkSession, 1800);
    }
  }
}
function expireSession() {
  if (blockedLogout.value) return;
  revision++;
  pending?.abort();
  clearTimeout(timer);
  clearWorkspace();
  authError.value = "로그인이 만료되었습니다. Google 계정으로 다시 로그인하세요.";
  checking.value = false;
  timer = setTimeout(checkSession, 1800);
}
function suspendSession() {
  revision++;
  pending?.abort();
  clearTimeout(timer);
  clearWorkspace();
  checking.value = false;
}
function broadcastLogout(notice: LogoutNotice) {
  localStorage.setItem("beatit-auth-change", JSON.stringify(notice));
}
async function logout() {
  if (signingOut.value) return;
  const ownerId = user.value?.id || blockedLogout.value?.ownerId;
  if (!ownerId) return;
  const attempt: LogoutNotice = { ownerId, nonce: crypto.randomUUID(), phase: "pending" };
  signingOut.value = true;
  blockedLogout.value = attempt;
  authError.value = "";
  suspendSession();
  // Hold every affected tab signed out until this exact attempt is reconciled.
  broadcastLogout(attempt);
  await nextTick();
  try {
    const response = await fetch("/api/auth/logout", {
      method: "POST", credentials: "same-origin", cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) throw new Error(logoutFailure);
    blockedLogout.value = null;
    authError.value = "";
    broadcastLogout({ ...attempt, phase: "completed" });
  } catch {
    blockedLogout.value = { ...attempt, phase: "error" };
    authError.value = logoutFailure;
    broadcastLogout(blockedLogout.value);
  } finally {
    signingOut.value = false;
    checking.value = false;
  }
}
function onStorage(event: StorageEvent) {
  if (event.key !== "beatit-auth-change" || !event.newValue) return;
  let notice: LogoutNotice;
  try {
    notice = JSON.parse(event.newValue);
    if (!notice || typeof notice.ownerId !== "string" || !notice.ownerId ||
        typeof notice.nonce !== "string" || !notice.nonce ||
        !["pending", "completed", "error"].includes(notice.phase)) return;
  } catch { return; }
  if (notice.phase === "pending") {
    // A delayed notice from another account cannot clear a newly signed-in user.
    if (signingOut.value || (user.value && user.value.id !== notice.ownerId) ||
        (blockedLogout.value && blockedLogout.value.ownerId !== notice.ownerId)) return;
    blockedLogout.value = notice;
    authError.value = "";
    suspendSession();
    return;
  }
  if (blockedLogout.value?.ownerId !== notice.ownerId ||
      blockedLogout.value?.nonce !== notice.nonce) return;
  suspendSession();
  if (notice.phase === "error") {
    blockedLogout.value = notice;
    authError.value = logoutFailure;
  } else {
    blockedLogout.value = null;
    authError.value = "";
    void checkSession();
  }
}
function onFocus() {
  void checkSession();
}
onMounted(() => {
  localStorage.removeItem("beatit-session");
  const params = new URLSearchParams(location.search);
  if (params.get("auth_error") === "cancelled") {
    authError.value = "Google 로그인이 취소되었습니다. 다시 로그인할 수 있습니다.";
    history.replaceState(null, "", location.pathname);
  }
  window.addEventListener("focus", onFocus);
  window.addEventListener("storage", onStorage);
  window.addEventListener("beatit-session-expired", expireSession);
  void checkSession();
});
onBeforeUnmount(() => {
  disposed = true;
  revision++;
  pending?.abort();
  clearTimeout(timer);
  window.removeEventListener("focus", onFocus);
  window.removeEventListener("storage", onStorage);
  window.removeEventListener("beatit-session-expired", expireSession);
});
</script>

<template>
  <UApp :toaster="{ position: 'bottom-right' }">
    <StudioWorkspace
      v-if="user"
      :key="user.id"
      :user="user"
      @logout="logout"
      @unauthorized="expireSession"
    />
    <main v-else class="min-h-dvh bg-default flex items-center justify-center px-6 py-12">
      <UCard class="w-full max-w-md" :ui="{ body: 'p-8 sm:p-10' }">
        <div class="space-y-6 text-center">
          <UIcon name="i-lucide-audio-lines" class="size-10 text-primary" />
          <div class="space-y-3">
            <p class="text-xs tracking-[0.2em] text-muted">BEATIT STUDIO</p>
            <h1 class="text-2xl font-semibold text-highlighted">내 드럼 악보 작업실</h1>
            <p class="text-sm leading-6 text-muted">오디오를 드럼 악보로 만들고, 원음과 비교하며 다듬으세요. Google 계정으로 나만의 프로젝트를 저장하고 이어서 작업할 수 있습니다.</p>
          </div>
          <div v-if="checking" role="status" class="flex items-center justify-center gap-2 text-sm text-muted">
            <UIcon name="i-lucide-loader-circle" class="animate-spin" /> 로그인 확인 중
          </div>
          <template v-else>
            <UAlert v-if="authError" :title="authError" color="error" variant="soft" />
            <UButton
              v-if="blockedLogout?.phase === 'error'"
              label="로그아웃 다시 시도"
              color="neutral"
              variant="outline"
              block
              :loading="signingOut"
              @click="logout"
            />
            <div v-if="logoutPending" role="status" class="flex items-center justify-center gap-2 text-sm text-muted">
              <UIcon name="i-lucide-loader-circle" class="animate-spin" /> 로그아웃 중
            </div>
            <UButton
              label="Google로 시작하기"
              icon="i-lucide-log-in"
              href="/api/auth/google"
              external
              size="lg"
              block
              :disabled="logoutPending"
            />
            <p class="text-xs leading-5 text-dimmed">처음 로그인하면 계정이 만들어집니다.<br>프로젝트와 파일은 내 계정에서만 볼 수 있습니다.</p>
          </template>
        </div>
      </UCard>
    </main>
  </UApp>
</template>
