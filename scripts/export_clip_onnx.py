"""Export a CLIP model's image and text encoders from open_clip to ONNX (plan §5).

    uv run python ../scripts/export_clip_onnx.py --out ~/.cache/photo-triage/models/clip

Run from `backend/`, with the `export` dependency group (torch, open_clip, onnx),
which the runtime image leaves out. `scripts/bootstrap.sh` runs it. It writes, into
`OUT/<model>/`:

- `image.onnx`: float32 pixels [batch, 3, size, size] -> embeddings [batch, dim];
- `text.onnx`: int64 token ids [batch, context] -> embeddings [batch, dim];
- the tokenizer's BPE vocabulary, and `clip.json` with the preprocessing settings,
  so `photo_triage.ml.clip` needs neither torch nor open_clip.

Embeddings come out as the model gives them; the adapter L2-normalises them. Both
models have a dynamic batch axis. The pretrained weights are open_clip's download,
which checks their SHA-256.

Files are written over any already there, `clip.json` last, so on a first export its
presence means the export finished. Nothing is deleted (dev-environment §7).
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, cast

import open_clip  # pyright: ignore[reportMissingTypeStubs]
import torch
from open_clip import tokenizer as clip_tokenizer  # pyright: ignore[reportMissingTypeStubs]

PRETRAINED = {"ViT-B-32": "openai"}
"""The weights for each `CLIP_MODEL`. OpenAI's, since the aesthetic predictor (plan
§6.8) was trained on their ViT-B/32 embeddings."""
OPSET = 18


class ImageEncoder(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, pixels: torch.Tensor) -> torch.Tensor:
        return self.model.encode_image(pixels)  # type: ignore[operator]


class TextEncoder(torch.nn.Module):
    def __init__(self, model: torch.nn.Module) -> None:
        super().__init__()
        self.model = model

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        return self.model.encode_text(tokens)  # type: ignore[operator]


def export(name: str, out: Path) -> Path:
    """Write `name`'s models to `out/<name>/`, over what's there. Returns the folder."""
    pretrained = PRETRAINED[name]
    model, _, preprocess = open_clip.create_model_and_transforms(name, pretrained=pretrained)
    model.eval()
    visual = cast(Any, model).visual
    size = int(visual.image_size[0] if isinstance(visual.image_size, tuple) else visual.image_size)
    tokenizer = cast(Any, open_clip.get_tokenizer(name))
    context = int(tokenizer.context_length)
    normalize = cast(Any, preprocess).transforms[-1]

    target = out / name
    target.mkdir(parents=True, exist_ok=True)

    batch = torch.export.Dim("batch", min=1, max=1024)
    # A batch of 2: with 1, the exporter would fix the batch size at 1.
    pixels = torch.randn(2, 3, size, size)
    tokens = tokenizer(["a photo", "a photo of a dog"])
    with torch.no_grad():
        dim = int(cast(Any, model).encode_image(pixels).shape[-1])
        for module, example, input_name, file in (
            (ImageEncoder(model), pixels, "pixels", "image.onnx"),
            (TextEncoder(model), tokens, "tokens", "text.onnx"),
        ):
            program = torch.onnx.export(
                module,
                (example,),
                dynamo=True,
                input_names=[input_name],
                output_names=["embeddings"],
                dynamic_shapes={input_name: {0: batch}},
                opset_version=OPSET,
            )
            assert program is not None
            program.save(str(target / file), external_data=False)

    shutil.copyfile(clip_tokenizer.default_bpe(), target / "bpe_simple_vocab_16e6.txt.gz")
    config = {
        "model": name,
        "pretrained": pretrained,
        "open_clip_version": open_clip.__version__,
        "image_size": size,
        "mean": list(normalize.mean),
        "std": list(normalize.std),
        "context_length": context,
        "embedding_dim": dim,
    }
    (target / "clip.json").write_text(json.dumps(config, indent=2) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True, help="the models/clip folder")
    parser.add_argument("--model", default="ViT-B-32", choices=sorted(PRETRAINED))
    args = parser.parse_args(argv)
    target = export(args.model, args.out.expanduser())
    print(f"Exported {args.model} to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
