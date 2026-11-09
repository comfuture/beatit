<script setup lang="ts">
const projects = [
  { name: "Nuxt", role: "웹 프레임워크", url: "https://github.com/nuxt/nuxt" },
  { name: "Nuxt UI", role: "인터페이스 컴포넌트", url: "https://github.com/nuxt/ui" },
  { name: "Vue", role: "반응형 인터페이스", url: "https://github.com/vuejs/core" },
  { name: "Tailwind CSS", role: "스타일링", url: "https://github.com/tailwindlabs/tailwindcss" },
  { name: "FastAPI", role: "서버 API", url: "https://github.com/fastapi/fastapi" },
  { name: "PyTorch", role: "오디오 모델 실행", url: "https://github.com/pytorch/pytorch" },
  { name: "Demucs", role: "드럼 소스 분리", url: "https://github.com/adefossez/demucs" },
  { name: "DrumSep", role: "드럼 악기별 분리", url: "https://github.com/xavriley/mdx23c-drum-separation" },
  { name: "ADTOF-pytorch", role: "드럼 타격 검출", url: "https://github.com/xavriley/ADTOF-pytorch" },
  { name: "Beat This!", role: "박자 추적", url: "https://github.com/CPJKU/beat_this" },
  { name: "Verovio", role: "악보 렌더링", url: "https://github.com/rism-digital/verovio" },
  { name: "FFmpeg", role: "오디오·비디오 처리", url: "https://github.com/FFmpeg/FFmpeg" },
];
</script>

<template>
  <UModal
    title="Credits"
    description="Beatit을 함께 만드는 오픈 소스 프로젝트입니다."
    :ui="{ content: 'sm:max-w-lg', body: 'p-4 sm:p-5', footer: 'justify-center p-4 sm:p-5' }"
  >
    <button type="button" class="credits-trigger">Credits</button>
    <template #close="{ ui }">
      <UButton icon="i-lucide-x" color="neutral" variant="ghost" aria-label="닫기" :class="ui.close()" />
    </template>
    <template #body>
      <ul class="grid grid-cols-2 gap-2" aria-label="오픈 소스 프로젝트">
        <li v-for="project in projects" :key="project.name" class="min-w-0">
          <a
            :href="project.url"
            target="_blank"
            rel="noopener noreferrer"
            :aria-label="`${project.name} (새 탭에서 열기)`"
            class="flex h-full items-center gap-2 rounded-md border border-default px-3 py-2.5 text-sm transition-colors hover:bg-elevated focus-visible:outline-2 focus-visible:outline-primary focus-visible:outline-offset-2"
          >
            <span class="min-w-0 flex-1">
              <span class="block text-xs font-medium text-highlighted">{{ project.name }}</span>
              <span class="mt-0.5 block text-[10px] text-muted">{{ project.role }}</span>
            </span>
            <UIcon name="i-lucide-arrow-up-right" class="size-3 shrink-0 text-dimmed" aria-hidden="true" />
          </a>
        </li>
      </ul>
    </template>
    <template #footer>
      <p class="text-center text-xs text-muted">Made for Hayeon with ❤️ - papa</p>
    </template>
  </UModal>
</template>
