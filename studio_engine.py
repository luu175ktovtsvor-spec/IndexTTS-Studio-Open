"""Shared Apple-silicon runtime for the local IndexTTS interfaces."""

from __future__ import annotations

import torch
from torch import nn

from indextts.infer_v2_5 import IndexTTS2


class CpuVocoder(nn.Module):
    """Keep the known MPS-incompatible BigVGAN operation on CPU only."""

    def __init__(self, vocoder: nn.Module) -> None:
        super().__init__()
        self.vocoder = vocoder.to("cpu").eval()

    def forward(self, mel: torch.Tensor) -> torch.Tensor:
        return self.vocoder(mel.to("cpu"))


class MacIndexTTS2(IndexTTS2):
    """The original IndexTTS model with no model or prompt-path changes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self.device == "mps":
            self.bigvgan = CpuVocoder(self.bigvgan)
            print(
                ">> Apple Silicon mode: MPS is used for generation; "
                "the final BigVGAN vocoder runs on CPU for compatibility."
            )
