<script setup lang="ts">
import type { Options, SystemInfo } from "~/types/studio";
const model = defineModel<Options>({ required: true });
defineProps<{
  system: SystemInfo | null;
  revision?: boolean;
  disabled?: boolean;
}>();
function numberValue(value: string | number): number | null {
  return value === "" ? null : Number(value);
}
</script>

<template>
  <fieldset :disabled="disabled" class="options-fields">
    <div class="field-pair">
      <UFormField label="템포" :hint="revision ? 'BPM' : '자동 감지'">
        <UInput
          :model-value="model.bpm === null ? '' : String(model.bpm)"
          type="number"
          :min="30"
          :max="300"
          :step="0.1"
          placeholder="Auto"
          aria-label="템포 BPM"
          class="w-full"
          @update:model-value="model.bpm = numberValue($event)"
        />
      </UFormField>
      <UFormField label="박자">
        <USelect
          v-model="model.meter"
          :items="['4/4', '3/4', '6/8', '7/8']"
          aria-label="박자"
          class="w-full"
        />
      </UFormField>
    </div>
    <UFormField label="리듬 격자">
      <USelect
        v-model="model.grid"
        :items="[
          { label: '1/16 · 16분음표', value: '16' },
          { label: '1/8 · 8분음표', value: '8' },
          {
            label: '1/8T · 셋잇단음표',
            value: 'triplet',
            disabled: model.meter === '7/8',
          },
        ]"
        aria-label="리듬 격자"
        class="w-full"
      />
    </UFormField>
    <UFormField label="첫 마디 시작" hint="초">
      <UInput
        :model-value="model.offset === null ? '' : String(model.offset)"
        type="number"
        :min="-10"
        :max="120"
        :step="0.001"
        placeholder="자동 감지"
        aria-label="첫 마디 시작"
        class="w-full"
        @update:model-value="model.offset = numberValue($event)"
      />
    </UFormField>
    <template v-if="!revision">
      <UFormField label="입력 종류">
        <USelect
          v-model="model.source"
          :items="[
            { label: '전체 곡 · 드럼 분리', value: 'mix' },
            { label: '드럼 트랙', value: 'drums' },
          ]"
          aria-label="입력 종류"
          class="w-full"
        />
      </UFormField>
      <UCollapsible class="advanced-options">
        <UButton
          label="고급 설정"
          trailing-icon="i-lucide-chevron-down"
          color="neutral"
          variant="ghost"
          block
          class="justify-between"
        />
        <template #content>
          <div class="options-fields pt-3">
            <UFormField label="실행 장치"
              ><USelect
                v-model="model.device"
                :items="[
                  { label: '자동 선택', value: 'auto' },
                  ...(system?.devices || ['cpu']).map((d) => ({
                    label: system?.execution_backend === 'colab' && d === 'cuda'
                      ? 'Colab GPU · CUDA'
                      : d.toUpperCase(),
                    value: d,
                  })),
                ]"
                aria-label="실행 장치"
                class="w-full"
            /></UFormField>
            <UFormField label="검출 감도"
              ><USelect
                v-model="model.sensitivity"
                :items="[
                  { label: '낮음', value: 0.75 },
                  { label: '표준', value: 1 },
                  { label: '높음', value: 1.25 },
                ]"
                aria-label="검출 감도"
                class="w-full"
            /></UFormField>
            <UFormField label="악보 렌더러"
              ><USelect
                v-model="model.renderer"
                :items="[
                  { label: '자동 선택', value: 'auto' },
                  { label: 'MuseScore', value: 'musescore' },
                  { label: 'Verovio', value: 'verovio' },
                ]"
                aria-label="악보 렌더러"
                class="w-full"
            /></UFormField>
          </div>
        </template>
      </UCollapsible>
    </template>
    <USwitch
      v-model="model.tom_refinement"
      label="탐 높이 보정"
      description="고음·저음 구분 · 실험적 기능"
      size="sm"
    />
  </fieldset>
</template>
