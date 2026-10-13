import type { Ref, ComputedRef } from "vue";
import type { DrumEvent, Grid } from "~/types/studio";
import { barPlaybackRange, DrumSynth, firstPlaybackHit, playbackHits, timePlaybackRange, type PlaybackRange } from "~/utils/drumPlayback";

export function usePlaybackTransport(
  player: Readonly<Ref<HTMLAudioElement | null>>,
  events: ComputedRef<DrumEvent[]>,
  grid: ComputedRef<Grid>,
  duration: ComputedRef<number>,
  emitTime: (time: number) => void,
  emitNavigate: (time: number) => void,
) {
  const position = ref(0);
  const playing = ref(false);
  const audioEnabled = ref(true);
  const drumsEnabled = ref(false);
  const audioVolume = ref(80);
  const drumVolume = ref(80);
  const rate = ref(1);
  const range = ref<PlaybackRange | null>(null);
  const repeatEnabled = ref(false);
  const pendingEdits = ref(false);
  const error = ref("");
  const canPlay = computed(() => audioEnabled.value || drumsEnabled.value);
  const notes = computed(() => playbackHits(events.value, grid.value, duration.value));
  // Keep each loop pass stable while the editor continues changing the draft.
  let passNotes = notes.value;
  let context: AudioContext | undefined;
  let synth: DrumSynth | undefined;
  let mediaSource: MediaElementAudioSourceNode | undefined;
  let mediaVolume: GainNode | undefined;
  let mediaGate: GainNode | undefined;
  let drumGate: GainNode | undefined;
  let mix: DynamicsCompressorNode | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  let frame: number | undefined;
  let running = false;
  let anchorTime = 0;
  let anchorPosition = 0;
  let cursor = 0;
  let operation = 0;
  let disposed = false;
  let sourceLoading = false;
  let resumeSource = false;
  const end = () => range.value?.end ?? duration.value;
  const start = () => range.value?.start ?? 0;

  function publish(value: number) {
    position.value = Math.max(0, Math.min(duration.value, value));
    emitTime(position.value);
  }
  function clock(withAudio = audioEnabled.value, speed = rate.value) {
    if (withAudio && player.value && !sourceLoading) return player.value.currentTime;
    return running && context
      ? anchorPosition + (context.currentTime - anchorTime) * speed
      : position.value;
  }
  function ensureAudio() {
    if (context) return context;
    context = new AudioContext();
    mix = context.createDynamicsCompressor();
    mix.threshold.value = -3;
    mix.knee.value = 0;
    mix.ratio.value = 20;
    mix.attack.value = 0.003;
    mix.release.value = 0.08;
    mix.connect(context.destination);
    synth = new DrumSynth(context);
    drumGate = context.createGain();
    synth.output.connect(drumGate).connect(mix);
    mediaGate = context.createGain();
    mediaGate.gain.value = 0;
    mediaSource = context.createMediaElementSource(player.value!);
    mediaVolume = context.createGain();
    mediaSource.connect(mediaVolume).connect(mediaGate).connect(mix);
    applyVolumes();
    return context;
  }
  function applyVolumes() {
    if (!context) return;
    const now = context.currentTime;
    for (const [gain, value] of [[mediaVolume, audioVolume.value / 100],
      [synth?.output, 0.65 * drumVolume.value / 100]] as const) {
      if (!gain) continue;
      // Smooth level changes without altering the clock, notes or range gates.
      gain.gain.cancelAndHoldAtTime(now);
      gain.gain.setTargetAtTime(value, now, 0.01);
    }
  }
  function gateOutputs() {
    if (!context) return;
    const now = context.currentTime;
    const cutoff = now + Math.max(0, end() - clock()) / rate.value;
    for (const [gate, enabled] of [[mediaGate, audioEnabled.value], [drumGate, drumsEnabled.value]] as const) {
      if (!gate) continue;
      gate.gain.cancelScheduledValues(now);
      gate.gain.setValueAtTime(running && enabled ? 1 : 0, now);
      // The audio clock cuts both tracks and drum tails at the region boundary,
      // even when browser timers/UI frames are delayed in a background tab.
      if (running) gate.gain.setValueAtTime(0, cutoff);
    }
  }
  function stopClock() {
    running = false;
    clearInterval(timer);
    timer = undefined;
    if (frame !== undefined) cancelAnimationFrame(frame);
    frame = undefined;
    synth?.stop();
    gateOutputs();
  }
  function resetNotes(at = running ? clock() : position.value) {
    synth?.stop();
    cursor = firstPlaybackHit(passNotes, at);
  }
  function useDraftNotes() {
    passNotes = notes.value;
    pendingEdits.value = false;
  }
  function finish() {
    const repeat = repeatEnabled.value && range.value && canPlay.value;
    pause();
    setPosition(range.value ? range.value.start : duration.value);
    if (repeat) void play();
  }
  function pump() {
    if (!running || !context) return;
    const now = clock();
    if (now >= end() - 0.001) { finish(); return; }
    if (!drumsEnabled.value) return;
    const horizon = Math.min(end(), now + 0.12 * rate.value);
    while (cursor < passNotes.length && passNotes[cursor]!.time < horizon) {
      const hit = passNotes[cursor++]!;
      if (hit.time >= now - 0.04 && hit.time < end())
        synth?.hit(hit, context.currentTime + Math.max(0, hit.time - now) / rate.value);
    }
  }
  function startClock() {
    if (!playing.value || disposed || running || !context) return;
    // Let media seeking/buffering settle before starting the matching drums.
    if (audioEnabled.value && (sourceLoading || player.value?.seeking)) return;
    anchorPosition = position.value;
    anchorTime = context.currentTime;
    running = true;
    // The audio clock can advance during startup. Include the anchor's first
    // note rather than searching from a slightly later second clock reading.
    resetNotes(anchorPosition);
    gateOutputs();
    pump();
    if (!running) return;
    timer = setInterval(pump, 25);
    const sample = () => {
      if (!running) return;
      const now = clock();
      if (now >= end() - 0.001) { finish(); return; }
      publish(now);
      frame = requestAnimationFrame(sample);
    };
    frame = requestAnimationFrame(sample);
  }
  function setPosition(value: number) {
    publish(value);
    if (player.value && player.value.readyState >= 1)
      player.value.currentTime = position.value;
  }
  async function play(refreshNotes = true) {
    if (!canPlay.value || disposed) return;
    const token = ++operation;
    try {
      const audioContext = ensureAudio();
      await audioContext.resume();
      if (token !== operation || disposed) return;
      if (refreshNotes || !repeatEnabled.value || !range.value) useDraftNotes();
      if (position.value < start() || position.value >= end() - 0.001) setPosition(start());
      error.value = "";
      playing.value = true;
      if (audioEnabled.value) {
        if (!player.value) return;
        player.value.currentTime = position.value;
        player.value.playbackRate = rate.value;
        await player.value.play();
        if (token !== operation || disposed) return;
        startClock();
      } else startClock();
    } catch {
      if (token !== operation || disposed) return;
      pause();
      error.value = audioEnabled.value
        ? "오디오를 재생할 수 없습니다. SOURCE를 확인하거나 오디오를 끄고 드럼비트만 재생하세요."
        : "드럼비트를 재생할 수 없습니다. 브라우저의 오디오 사용을 확인하세요.";
    }
  }
  function pause() {
    operation++;
    resumeSource = false;
    const now = clock();
    playing.value = false;
    stopClock();
    player.value?.pause();
    publish(audioEnabled.value && !sourceLoading && player.value ? player.value.currentTime : now);
  }
  function toggle() { if (playing.value) pause(); else void play(); }
  function seek(value: number) {
    if (!Number.isFinite(value)) return;
    const resume = playing.value;
    pause();
    if (range.value && (value < range.value.start || value >= range.value.end)) range.value = null;
    setPosition(value);
    emitNavigate(position.value);
    if (resume) void play();
  }
  function stop() { pause(); setPosition(start()); }
  function playBar(bar: number) {
    pause();
    range.value = barPlaybackRange(bar, grid.value, duration.value);
    if (!range.value) { error.value = "이 마디에는 재생할 구간이 없습니다."; return; }
    setPosition(range.value.start);
    void play();
  }
  function clearRange() {
    range.value = null;
    if (running) gateOutputs();
  }
  function selectRange(first: number, last: number) {
    const selected = timePlaybackRange(first, last, duration.value);
    if (!selected) return;
    const resume = playing.value;
    pause();
    range.value = selected;
    setPosition(selected.start);
    emitNavigate(position.value);
    if (resume) void play();
  }
  function sourceChanged() {
    if (!audioEnabled.value) return;
    const resume = playing.value || resumeSource;
    pause();
    resumeSource = resume;
    sourceLoading = true;
  }
  function mediaReady() {
    sourceLoading = false;
    applyVolumes();
    if (!player.value) return;
    player.value.playbackRate = rate.value;
    player.value.currentTime = position.value;
    if (resumeSource && audioEnabled.value) {
      resumeSource = false;
      void play(false);
    }
  }
  function mediaPlaying() {
    if (!audioEnabled.value || !playing.value || player.value?.paused) return;
    sourceLoading = false;
    publish(player.value?.currentTime || 0);
    startClock();
  }
  function mediaWaiting() {
    if (!audioEnabled.value) return;
    publish(clock());
    stopClock();
  }
  function mediaTime() {
    if (!audioEnabled.value || sourceLoading || !player.value || player.value.readyState < 1) return;
    if (playing.value && clock() >= end() - 0.001) finish();
    else publish(player.value.currentTime);
  }
  function mediaSeeked() {
    mediaTime();
    if (audioEnabled.value && player.value && !player.value.paused) startClock();
  }
  function mediaPaused() {
    if (audioEnabled.value && playing.value && player.value?.paused && !sourceLoading) pause();
  }
  function mediaEnded() {
    if (audioEnabled.value && playing.value && player.value?.ended) finish();
  }
  function mediaError() {
    if (!audioEnabled.value) return;
    pause();
    sourceLoading = false;
    error.value = "선택한 오디오를 불러오지 못했습니다. 오디오를 끄면 드럼비트만 재생할 수 있습니다.";
  }
  watch(audioEnabled, (_enabled, before) => {
    const now = clock(before);
    const resume = playing.value || resumeSource;
    playing.value = false;
    operation++;
    stopClock();
    player.value?.pause();
    sourceLoading = false;
    resumeSource = false;
    setPosition(now);
    if (resume && canPlay.value) void play(false);
  });
  watch(drumsEnabled, () => {
    if (!canPlay.value) { pause(); return; }
    resetNotes();
    gateOutputs();
    if (running) pump();
  });
  watch(rate, (_speed, before) => {
    const now = clock(audioEnabled.value, before);
    const wasRunning = running;
    stopClock();
    publish(now);
    if (player.value) player.value.playbackRate = rate.value;
    if (wasRunning) startClock();
  });
  watch([audioVolume, drumVolume], applyVolumes);
  watch(notes, () => {
    if (repeatEnabled.value && range.value && (playing.value || resumeSource)) {
      pendingEdits.value = true;
      return;
    }
    useDraftNotes();
    resetNotes();
    if (running) pump();
  });
  watch(repeatEnabled, (repeat) => {
    if (!repeat && pendingEdits.value) {
      useDraftNotes();
      resetNotes();
      if (running) pump();
    }
  });
  watch(range, (selected) => { if (!selected) repeatEnabled.value = false; });
  watch(() => JSON.stringify([grid.value.bpm, grid.value.offset, grid.value.meter, grid.value.step]),
    () => { stop(); range.value = null; });
  onBeforeUnmount(() => {
    disposed = true;
    pause();
    synth?.dispose();
    mediaSource?.disconnect();
    mediaVolume?.disconnect();
    mediaGate?.disconnect();
    drumGate?.disconnect();
    mix?.disconnect();
    void context?.close();
  });
  return {
    position, playing, audioEnabled, drumsEnabled, audioVolume, drumVolume, rate, range, repeatEnabled, pendingEdits, error, canPlay,
    toggle, pause, seek, stop, playBar, clearRange, selectRange, sourceChanged,
    mediaReady, mediaPlaying, mediaWaiting, mediaTime, mediaSeeked, mediaPaused, mediaEnded, mediaError,
  };
}
