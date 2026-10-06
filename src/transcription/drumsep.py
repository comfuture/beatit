"""Drum-kit stem separation with the MDX23C DrumSep model.

The network definition is adapted from the MIT-licensed MDX23C implementation in
ZFTurbo/Music-Source-Separation-Training and xavriley/mdx23c-drum-separation
(commit 80ae44e3d2c091e7413512bfb2a2ec7c989a095c). The checkpoint is jarredou's
5-stem DrumSep model, mirrored on Hugging Face and pinned by revision and SHA-256.
Weights are downloaded into the user's cache at first use and are not vendored.
"""

import hashlib
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import numpy as np

REPOSITORY = "xavriley/source_separation_mirror"
REVISION = "da6dc5af5458a296e04cbf52b97babced90eb908"
CHECKPOINT = "drumsep_5stems_mdx23c_jarredou.ckpt"
SHA256 = "1f8e636fb674b88a52c8399fde9a4ebe2b72b065ca07eed4e03ab1c9f0bfb2e0"
STEMS = ("kick", "snare", "toms", "hh", "cymbals")
SAMPLE_RATE = 44100
# Values from config_mdx23c_drumsep2025.yaml at the pinned revision.
CONFIG = SimpleNamespace(
    n_fft=2048,
    hop_length=512,
    dim_f=1024,
    num_channels=2,
    chunk_size=523776,
    num_overlap=4,
    bottleneck_factor=4,
    growth=128,
    num_blocks_per_scale=2,
    channels=128,
    num_scales=5,
    num_subbands=4,
    scale=(2, 2),
)


def checkpoint_path(download: bool = True) -> Path:
    from huggingface_hub import hf_hub_download

    path = Path(
        hf_hub_download(REPOSITORY, CHECKPOINT, revision=REVISION, local_files_only=not download)
    )
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(1 << 22):
            digest.update(block)
    if digest.hexdigest() != SHA256:
        raise RuntimeError(f"DrumSep 가중치의 SHA-256이 일치하지 않습니다: {path}")
    return path


def build_model():
    import torch
    from torch import nn

    config = CONFIG
    norm = partial(nn.InstanceNorm2d, affine=True)

    class STFT:
        def __init__(self):
            self.window = torch.hann_window(config.n_fft, periodic=True)

        def __call__(self, x):
            window = self.window.to(x.device)
            batch, channels, samples = x.shape
            spec = torch.stft(
                x.reshape(-1, samples),
                n_fft=config.n_fft,
                hop_length=config.hop_length,
                window=window,
                center=True,
                return_complex=True,
            )
            spec = torch.view_as_real(spec).permute(0, 3, 1, 2)
            spec = spec.reshape(batch, channels * 2, -1, spec.shape[-1])
            return spec[..., : config.dim_f, :]

        def inverse(self, x):
            window = self.window.to(x.device)
            batch_dims = x.shape[:-3]
            channels, frequencies, frames = x.shape[-3:]
            bins = config.n_fft // 2 + 1
            pad = torch.zeros([*batch_dims, channels, bins - frequencies, frames], device=x.device)
            x = torch.cat([x, pad], -2)
            x = x.reshape([-1, 2, bins, frames]).permute(0, 2, 3, 1).contiguous()
            x = torch.view_as_complex(x)
            x = torch.istft(x, n_fft=config.n_fft, hop_length=config.hop_length, window=window)
            return x.reshape([*batch_dims, 2, -1])

    class Scale(nn.Module):
        def __init__(self, inputs, outputs, transpose):
            super().__init__()
            layer = nn.ConvTranspose2d if transpose else nn.Conv2d
            self.conv = nn.Sequential(
                norm(inputs),
                nn.GELU(),
                layer(inputs, outputs, config.scale, config.scale, bias=False),
            )

        def forward(self, x):
            return self.conv(x)

    class TFCTDF(nn.Module):
        def __init__(self, inputs, channels, frequencies):
            super().__init__()
            self.blocks = nn.ModuleList()
            bottleneck = frequencies // config.bottleneck_factor
            for _ in range(config.num_blocks_per_scale):
                block = nn.Module()
                block.tfc1 = nn.Sequential(
                    norm(inputs), nn.GELU(), nn.Conv2d(inputs, channels, 3, 1, 1, bias=False)
                )
                block.tdf = nn.Sequential(
                    norm(channels),
                    nn.GELU(),
                    nn.Linear(frequencies, bottleneck, bias=False),
                    norm(channels),
                    nn.GELU(),
                    nn.Linear(bottleneck, frequencies, bias=False),
                )
                block.tfc2 = nn.Sequential(
                    norm(channels), nn.GELU(), nn.Conv2d(channels, channels, 3, 1, 1, bias=False)
                )
                block.shortcut = nn.Conv2d(inputs, channels, 1, 1, 0, bias=False)
                self.blocks.append(block)
                inputs = channels

        def forward(self, x):
            for block in self.blocks:
                shortcut = block.shortcut(x)
                x = block.tfc1(x)
                x = x + block.tdf(x)
                x = block.tfc2(x) + shortcut
            return x

    class TFCTDFNet(nn.Module):
        def __init__(self):
            super().__init__()
            subbands = config.num_subbands
            dim_c = subbands * config.num_channels * 2
            channels, growth = config.channels, config.growth
            frequencies = config.dim_f // subbands
            self.first_conv = nn.Conv2d(dim_c, channels, 1, 1, 0, bias=False)
            self.encoder_blocks = nn.ModuleList()
            for _ in range(config.num_scales):
                block = nn.Module()
                block.tfc_tdf = TFCTDF(channels, channels, frequencies)
                block.downscale = Scale(channels, channels + growth, transpose=False)
                frequencies //= config.scale[1]
                channels += growth
                self.encoder_blocks.append(block)
            self.bottleneck_block = TFCTDF(channels, channels, frequencies)
            self.decoder_blocks = nn.ModuleList()
            for _ in range(config.num_scales):
                block = nn.Module()
                block.upscale = Scale(channels, channels - growth, transpose=True)
                frequencies *= config.scale[1]
                channels -= growth
                block.tfc_tdf = TFCTDF(2 * channels, channels, frequencies)
                self.decoder_blocks.append(block)
            self.final_conv = nn.Sequential(
                nn.Conv2d(channels + dim_c, channels, 1, 1, 0, bias=False),
                nn.GELU(),
                nn.Conv2d(channels, len(STEMS) * dim_c, 1, 1, 0, bias=False),
            )
            self.stft = STFT()

        def forward(self, x):
            k = config.num_subbands
            x = self.stft(x)
            b, c, f, t = x.shape
            mix = x = x.reshape(b, c * k, f // k, t)
            first = x = self.first_conv(x)
            x = x.transpose(-1, -2)
            skips = []
            for block in self.encoder_blocks:
                x = block.tfc_tdf(x)
                skips.append(x)
                x = block.downscale(x)
            x = self.bottleneck_block(x)
            for block in self.decoder_blocks:
                x = block.upscale(x)
                x = block.tfc_tdf(torch.cat([x, skips.pop()], 1))
            x = x.transpose(-1, -2) * first
            x = self.final_conv(torch.cat([mix, x], 1))
            b, c, f, t = x.shape
            x = x.reshape(b, c // k, f * k, t).reshape(b, len(STEMS), -1, f * k, t)
            return self.stft.inverse(x)

    return TFCTDFNet()


def load_model(device: str = "cpu", download: bool = True):
    import torch

    model = build_model()
    state = torch.load(checkpoint_path(download), map_location="cpu", weights_only=True)
    # Strict loading: a config mismatch must fail instead of separating with random weights.
    model.load_state_dict(state.get("state_dict", state), strict=True)
    return model.to(device).eval()


def separate(
    audio: np.ndarray, model, device: str, output_directory: Path, progress=None
) -> dict[str, Path]:
    """Write float WAV stems with chunk-bounded overlap-add buffers.

    Input is shaped (2, samples) at 44.1 kHz. Completed prefixes cannot receive
    contributions from later chunks, so they can be normalized and written immediately.
    """
    import soundfile as sf
    import torch
    import torch.nn.functional as F

    audio = np.atleast_2d(audio)
    if audio.shape[0] == 1:
        audio = np.repeat(audio, 2, axis=0)
    mix = torch.from_numpy(np.asarray(audio, dtype=np.float32))
    chunk = CONFIG.chunk_size
    step = chunk // CONFIG.num_overlap
    fade = chunk // 10
    border = chunk - step
    length = mix.shape[-1]
    padded = length > 2 * border
    trim = border if padded else 0
    total = length + 2 * trim
    window = torch.ones(chunk)
    window[:fade] = torch.linspace(0, 1, fade)
    window[-fade:] = torch.linspace(1, 0, fade)
    result = torch.zeros((len(STEMS), 2, chunk))
    weight = torch.zeros(chunk)
    starts = range(0, total, step)
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {name: output_directory / f"{name}.wav" for name in STEMS}
    with ExitStack() as files, torch.inference_mode():
        writers = [
            files.enter_context(sf.SoundFile(path, "w", SAMPLE_RATE, 2, subtype="FLOAT"))
            for path in paths.values()
        ]
        for index, start in enumerate(starts):
            size = min(chunk, total - start)
            if padded:
                # Reflect only this chunk instead of copying the entire padded track.
                positions = torch.arange(start - trim, start - trim + size)
                positions = torch.where(positions < 0, -positions, positions)
                positions = torch.where(positions >= length, 2 * length - 2 - positions, positions)
                part = mix[:, positions]
            else:
                part = mix[:, start : start + size]
            mode = "reflect" if size > chunk // 2 else "constant"
            part = F.pad(part[None], (0, chunk - size), mode=mode)
            estimate = model(part.to(device))[0, ..., :size].float().cpu()
            # Keep full weight at the outer edges of the signal.
            current = window[:size].clone()
            if index == 0:
                current[:fade] = 1
            if index == len(starts) - 1:
                current[-min(fade, size) :] = 1
            result[..., :size] += estimate * current
            weight[:size] += current
            # Future chunks start at start + step. Keep their overlap, flush the rest.
            left, right = max(0, trim - start), min(step, size, trim + length - start)
            if right > left:
                completed = (result[..., left:right] / weight[left:right].clamp_min(1e-8)).numpy()
                np.nan_to_num(completed, copy=False)
                for writer, wave in zip(writers, completed):
                    writer.write(wave.T)
            result[..., :-step] = result[..., step:].clone()
            result[..., -step:] = 0
            weight[:-step] = weight[step:].clone()
            weight[-step:] = 0
            if progress:
                progress((index + 1) / len(starts))
    return paths
