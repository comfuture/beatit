<script setup lang="ts">
const audioEnabled = defineModel<boolean>("audioEnabled", { required: true });
const drumsEnabled = defineModel<boolean>("drumsEnabled", { required: true });
const audioVolume = defineModel<number>("audioVolume", { required: true });
const drumVolume = defineModel<number>("drumVolume", { required: true });
const trigger = useTemplateRef("trigger");
const popoverContent = {
  side: "top" as const,
  align: "start" as const,
  collisionPadding: 12,
  "aria-label": "재생 트랙 조절",
  onEscapeKeyDown: () => {
    void nextTick(() => trigger.value?.$el?.focus({ preventScroll: true }));
  },
  onCloseAutoFocus: (event: Event) => event.preventDefault(),
};
</script>

<template>
  <UPopover arrow :content="popoverContent">
    <template #default="{ open }">
      <UButton
        ref="trigger"
        aria-label="재생 트랙 조절"
        :aria-description="`오디오 ${audioVolume}%, ${audioEnabled ? '켜짐' : '꺼짐'} · 드럼비트 ${drumVolume}%, ${drumsEnabled ? '켜짐' : '꺼짐'}`"
        color="neutral"
        :variant="open ? 'soft' : 'outline'"
        size="xs"
        class="playback-mixer-button"
      >
        <span class="mixer-track-summary" data-track="audio" :data-enabled="audioEnabled">
          <UIcon name="i-lucide-audio-lines" /><span class="mixer-track-text">오디오 <span class="mono">{{ audioVolume }}%</span></span>
        </span>
        <span class="mixer-track-summary" data-track="drums" :data-enabled="drumsEnabled">
          <UIcon name="i-lucide-drum" /><span class="mixer-track-text">드럼비트 <span class="mono">{{ drumVolume }}%</span></span>
        </span>
      </UButton>
    </template>
    <template #content>
      <div class="playback-mixer-popover">
        <div class="mixer-track">
          <USwitch v-model="audioEnabled" label="오디오" size="xs" />
          <USlider
            v-model="audioVolume"
            orientation="vertical"
            :min="0"
            :max="100"
            aria-label="오디오 볼륨"
            :aria-valuetext="`${audioVolume}%`"
            class="track-volume-slider"
            size="sm"
          />
          <span class="mono">{{ audioVolume }}%</span>
        </div>
        <div class="mixer-track">
          <USwitch v-model="drumsEnabled" label="드럼비트" size="xs" />
          <USlider
            v-model="drumVolume"
            orientation="vertical"
            :min="0"
            :max="100"
            aria-label="드럼비트 볼륨"
            :aria-valuetext="`${drumVolume}%`"
            class="track-volume-slider"
            size="sm"
          />
          <span class="mono">{{ drumVolume }}%</span>
        </div>
      </div>
    </template>
  </UPopover>
</template>
