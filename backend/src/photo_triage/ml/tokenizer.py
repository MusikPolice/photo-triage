"""CLIP's byte-pair tokenizer, ported from open_clip's `SimpleTokenizer` so the runtime
needs neither torch nor open_clip (plan §5). It reads the vocabulary that
`scripts/export_clip_onnx.py` copies next to the models.

Only what CLIP's text model needs: open_clip's default `lower` cleaning, and
truncation to the context length with the end token kept last. A model-tier test
checks it gives open_clip's ids.

Adapted from open_clip (https://github.com/mlfoundations/open_clip), MIT licence:
Copyright (c) 2012-2021 Gabriel Ilharco, Mitchell Wortsman, Nicholas Carlini, Rohan
Taori, Achal Dave, Vaishaal Shankar, John Miller, Hongseok Namkoong, Hannaneh
Hajishirzi, Ali Farhadi, Ludwig Schmidt. Itself based on OpenAI's CLIP tokenizer.
"""

import functools
import gzip
import html
import itertools
from collections.abc import Sequence
from pathlib import Path

import ftfy
import numpy as np
import regex
from numpy.typing import NDArray

VOCAB_FILE = "bpe_simple_vocab_16e6.txt.gz"
_MERGES = 49152 - 256 - 2
"""How many merges of the vocabulary file CLIP uses."""
_START, _END = "<start_of_text>", "<end_of_text>"


@functools.cache
def _bytes_to_unicode() -> dict[int, str]:
    """A printable character for each byte, so BPE never sees whitespace or control
    characters."""
    printable = [
        *range(ord("!"), ord("~") + 1),
        *range(ord("¡"), ord("¬") + 1),
        *range(ord("®"), ord("ÿ") + 1),
    ]
    codes = printable[:]
    extra = 0
    for byte in range(256):
        if byte not in printable:
            printable.append(byte)
            codes.append(256 + extra)
            extra += 1
    return {byte: chr(code) for byte, code in zip(printable, codes, strict=True)}


def _clean(text: str) -> str:
    text = html.unescape(html.unescape(ftfy.fix_text(text))).strip()
    return " ".join(text.split()).lower()


class Tokenizer:
    def __init__(self, vocab_path: Path, context_length: int) -> None:
        with gzip.open(vocab_path, "rt", encoding="utf-8") as f:
            lines = f.read().split("\n")[1 : _MERGES + 1]
        merges = [tuple(line.split()) for line in lines]
        symbols = list(_bytes_to_unicode().values())
        vocab = [*symbols, *(s + "</w>" for s in symbols), *("".join(m) for m in merges)]
        vocab += [_START, _END]
        self._encoder = {token: index for index, token in enumerate(vocab)}
        self._ranks = {(m[0], m[1]): rank for rank, m in enumerate(merges)}
        self._cache: dict[str, str] = {_START: _START, _END: _END}
        self._pattern = regex.compile(
            rf"{_START}|{_END}|'s|'t|'re|'ve|'m|'ll|'d|[\p{{L}}]+|[\p{{N}}]|[^\s\p{{L}}\p{{N}}]+",
            regex.IGNORECASE,
        )
        self.context_length = context_length
        self._start_id, self._end_id = self._encoder[_START], self._encoder[_END]

    def __call__(self, texts: Sequence[str]) -> NDArray[np.int64]:
        """Token ids [len(texts), context_length], zero-padded."""
        ids = np.zeros((len(texts), self.context_length), dtype=np.int64)
        for row, text in enumerate(texts):
            tokens = [self._start_id, *self.encode(text), self._end_id]
            if len(tokens) > self.context_length:
                tokens = tokens[: self.context_length]
                tokens[-1] = self._end_id
            ids[row, : len(tokens)] = tokens
        return ids

    def encode(self, text: str) -> list[int]:
        """The ids of `text`'s tokens, without the start and end tokens."""
        byte_encoder = _bytes_to_unicode()
        ids: list[int] = []
        for match in self._pattern.findall(_clean(text)):
            token = "".join(byte_encoder[b] for b in str(match).encode("utf-8"))
            ids.extend(self._encoder[piece] for piece in self._bpe(token).split(" "))
        return ids

    def _bpe(self, token: str) -> str:
        if token in self._cache:
            return self._cache[token]
        word = (*token[:-1], token[-1] + "</w>")
        while len(word) > 1:
            pairs = set(itertools.pairwise(word))
            best = min(pairs, key=lambda pair: self._ranks.get(pair, float("inf")))
            if best not in self._ranks:
                break
            merged: list[str] = []
            i = 0
            while i < len(word):
                if i < len(word) - 1 and (word[i], word[i + 1]) == best:
                    merged.append(word[i] + word[i + 1])
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = tuple(merged)
        result = " ".join(word)
        self._cache[token] = result
        return result
