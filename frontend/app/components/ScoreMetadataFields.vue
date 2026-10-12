<script setup lang="ts">
import type { ScoreMetadata } from "~/types/studio";

const metadata = defineModel<ScoreMetadata>({ required: true });
defineProps<{ disabled: boolean; dirty: boolean }>();
defineEmits<{ apply: [] }>();
</script>

<template>
  <form aria-label="악보 정보" class="space-y-3" @submit.prevent="$emit('apply')">
    <div class="section-label">악보 정보</div>
    <UFormField label="악보 제목" name="score-title">
      <UInput
        v-model="metadata.title"
        :disabled="disabled"
        :maxlength="256"
        class="w-full"
        size="sm"
      />
    </UFormField>
    <UFormField label="저자" name="score-author">
      <UInput
        v-model="metadata.author"
        :disabled="disabled"
        :maxlength="256"
        placeholder="작곡가 또는 편곡자"
        class="w-full"
        size="sm"
      />
    </UFormField>
    <p class="text-xs text-muted leading-relaxed">
      제목은 세션 이름과 별도로 저장됩니다. 적용하면 악보와 내보내기 파일이 갱신됩니다.
    </p>
    <UButton
      type="submit"
      label="적용"
      icon="i-lucide-check"
      size="xs"
      :disabled="disabled || !dirty"
    />
  </form>
</template>
