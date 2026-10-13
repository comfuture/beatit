<script setup lang="ts">
const props = defineProps<{ label: string }>();
const volume = defineModel<number>({ required: true });
const trigger = useTemplateRef("trigger");
const popoverContent = computed(() => ({
  side: "top" as const,
  align: "center" as const,
  collisionPadding: 12,
  "aria-label": `${props.label} 볼륨 조절`,
  onEscapeKeyDown: () => {
    void nextTick(() => trigger.value?.$el?.focus({ preventScroll: true }));
  },
  // Delayed closing animations must not steal focus from another volume popover.
  onCloseAutoFocus: (event: Event) => event.preventDefault(),
}));
</script>

<template>
  <UPopover arrow :content="popoverContent">
    <template #default="{ open }">
      <UButton
        ref="trigger"
        :icon="volume === 0 ? 'i-lucide-volume-x' : 'i-lucide-volume-2'"
        :label="`${volume}%`"
        :aria-label="`${label} 볼륨 조절`"
        color="neutral"
        :variant="open ? 'soft' : 'ghost'"
        size="xs"
        class="track-volume-button"
      />
    </template>
    <template #content>
      <div class="track-volume-popover">
        <span>{{ label }}</span>
        <strong class="mono" aria-hidden="true">{{ volume }}%</strong>
        <USlider
          v-model="volume"
          orientation="vertical"
          :min="0"
          :max="100"
          :aria-label="`${label} 볼륨`"
          :aria-valuetext="`${volume}%`"
          class="track-volume-slider"
          size="sm"
        />
      </div>
    </template>
  </UPopover>
</template>
