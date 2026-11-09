<script setup lang="ts">
import type { SignedInUser } from "~/types/auth";
import { googleLoginPath } from "~/utils/sessionLocation";
import { isTransientReadError } from "~/utils/connection";

const user = shallowRef<SignedInUser | null>(null);
const checking = ref(true);
const locationPath = ref("/");
const loginPath = computed(() => googleLoginPath(locationPath.value));
function updateLocation() { locationPath.value = window.location.pathname; }
const signingOut = ref(false);
const authError = ref("");
const reconnecting = ref(false);
const connectionNotice = ref("");
let reconnectStarted = 0;
function resetReconnect() {
  reconnecting.value = false;
  connectionNotice.value = "";
  reconnectStarted = 0;
}
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
    if (disposed || request.signal.aborted || token !== revision) return;
    if (response.status === 401) {
      clearWorkspace();
      resetReconnect();
      authError.value = "로그인이 만료되었습니다. Google 계정으로 다시 로그인하세요.";
      checking.value = false;
      return;
    }
    if (!response.ok) throw Object.assign(new Error("Authentication check failed"), { status: response.status });
    const data: { user: SignedInUser | null } = await response.json();
    if (disposed || request.signal.aborted || token !== revision) return;
    if (data.user !== null && (!data.user || typeof data.user.id !== "string" || !data.user.id))
      throw Object.assign(new Error("Invalid authentication response"), { status: 400 });
    resetReconnect();
    if (data.user) {
      // Only a successful server response can establish or change a principal.
      user.value = data.user;
      authError.value = "";
    } else {
      const wasSignedIn = !!user.value;
      clearWorkspace();
      if (wasSignedIn)
        authError.value = "로그인이 만료되었습니다. Google 계정으로 다시 로그인하세요.";
    }
    checking.value = false;
  } catch (reason) {
    if (disposed || request.signal.aborted || token !== revision) return;
    if (isTransientReadError(reason)) {
      // Keep an already verified workspace and its draft. Every data request
      // continues to authenticate at the server; an unknown user stays gated.
      reconnecting.value = true;
      reconnectStarted ||= Date.now();
      if (Date.now() - reconnectStarted >= 4000)
        connectionNotice.value = "서버에 다시 연결 중…";
    } else {
      clearWorkspace();
      resetReconnect();
      authError.value = "로그인 응답을 확인할 수 없습니다. 다시 로그인하세요.";
      checking.value = false;
    }
  } finally {
    if (!disposed && token === revision && !signingOut.value && !blockedLogout.value)
      timer = setTimeout(checkSession, 1800);
  }
}
function expireSession() {
  if (blockedLogout.value) return;
  resetReconnect();
  revision++;
  pending?.abort();
  clearTimeout(timer);
  clearWorkspace();
  authError.value = "로그인이 만료되었습니다. Google 계정으로 다시 로그인하세요.";
  checking.value = false;
  timer = setTimeout(checkSession, 1800);
}
function suspendSession() {
  resetReconnect();
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
  updateLocation();
  window.addEventListener("popstate", updateLocation);
  window.addEventListener("beatit-location-change", updateLocation);
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
  window.removeEventListener("popstate", updateLocation);
  window.removeEventListener("beatit-location-change", updateLocation);
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
    <p v-if="user && connectionNotice" class="fixed top-2 left-1/2 -translate-x-1/2 z-50 rounded bg-muted px-3 py-1 text-xs text-muted" role="status">{{ connectionNotice }}</p>
    <main v-if="!user" class="min-h-dvh bg-default flex items-center justify-center px-6 py-12">
      <UCard class="w-full max-w-md" :ui="{ body: 'p-8 sm:p-10' }">
        <div class="space-y-6 text-center">
          <UIcon name="i-lucide-audio-lines" class="size-10 text-primary" />
          <h1 class="text-2xl font-semibold text-highlighted">Beatit</h1>
          <template v-if="checking">
            <div role="status" class="flex items-center justify-center gap-2 text-sm text-muted">
              <UIcon name="i-lucide-loader-circle" class="animate-spin" /> {{ reconnecting ? '서버에 연결 중…' : '로그인 확인 중' }}
            </div>
            <UButton v-if="reconnecting" label="다시 연결" variant="ghost" color="neutral" size="sm" @click="checkSession" />
          </template>
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
              label="Google로 로그인"
              icon="i-lucide-log-in"
              :href="loginPath"
              external
              size="lg"
              block
              :disabled="logoutPending"
            />
          </template>
        </div>
      </UCard>
    </main>
  </UApp>
</template>
