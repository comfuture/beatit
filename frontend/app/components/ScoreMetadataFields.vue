<script setup lang="ts">
import type { ScoreMetadata } from "~/types/studio";

const metadata = defineModel<ScoreMetadata>({ required: true });
defineProps<{ disabled: boolean; dirty: boolean }>();
defineEmits<{ apply: [] }>();
</script>

<template>
  <form aria-label="악보 정보" class="space-y-3" @submit.prevent="$emit('apply')">
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
    <UButton
      type="submit"
      label="적용"
      icon="i-lucide-check"
      size="xs"
      :disabled="disabled || !dirty"
    />
  </form>
</template>
