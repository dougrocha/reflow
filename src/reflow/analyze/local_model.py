"""The vision model that runs in this process: Qwen3-VL-8B through transformers on cuda, mps or
cpu (cpu works but is slow), behind the same `ask` as the HTTP transports of analyze.vision.

It loads lazily (14 s from the page cache, longer from disk; ~17 GB VRAM), then a median of 4.7 s
per figure on an RTX 3090 (docs/research/2026-11-alt-text-models.md). Alt text (analyze.enrich)
and transcription (analyze.assist) use it through `enrich.ModelProvider`.
"""

from __future__ import annotations

import gc
import io
import threading

from reflow import budget
from reflow.analyze.vision import (ProviderError, ProviderUnavailable, Reply, Usage, VisionImage,
                                   unload_ollama)
from reflow.providers import ALT_TEXT_MODELS
from reflow.device import resolve_device

MAX_IMAGE_SIDE = 1280  # px: figure crops are scaled down to this before the model sees them
_LOADED: dict[tuple[str, str], tuple] = {}  # (model id, device) -> (model, processor)
_MODEL_LOCK = threading.RLock()
_USERS: set[LocalModel] = set()   # the models that loaded and have not closed
_GPU: list[budget.GpuHold] = []   # the whole GPU lease, while a model is loaded on cuda


def free_cuda() -> None:
    """Torch keeps the memory of freed models cached; give it back to the card."""
    import torch
    gc.collect()
    torch.cuda.empty_cache()


class LocalModel:
    """One copy of the model per process, shared by every document (convert_many makes a model
    per document): a second 17 GB copy would not fit beside the first on a 24 GB card."""
    cache_id: str

    def __init__(self, model: str = ALT_TEXT_MODELS["local"], device: str = "auto") -> None:
        try:
            import torch  # noqa: F401
            import transformers  # noqa: F401
        except ImportError as e:
            raise ProviderUnavailable("local AI needs torch and transformers: " + str(e)) from e
        self.model_id, self.device = model, resolve_device(device)
        self.cache_id = model

    def load(self):
        """(model, processor), loading them on first use; ask calls it, and the benchmarks call it
        to keep the load out of what they time."""
        # On cuda it holds the whole GPU lease (reflow.budget) until the last model closes, and
        # asks Ollama to unload first: its qwen3-vl:8b (7.5 GB) and this model do not fit together.
        _USERS.add(self)
        key = (self.model_id, self.device)
        if key not in _LOADED:
            if self.device == "cuda" and not _GPU:
                _GPU.append(budget.hold_gpu(whole=True))
                unload_ollama()
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor
            processor = AutoProcessor.from_pretrained(self.model_id)
            model = AutoModelForImageTextToText.from_pretrained(
                self.model_id, dtype=torch.bfloat16).to(self.device)
            _LOADED[key] = (model, processor)
        return _LOADED[key]

    def close(self) -> None:
        """Done with the model. The last one to close unloads it and gives the GPU lease back.
        Safe to call twice, or without a load."""
        with _MODEL_LOCK:
            _USERS.discard(self)
            if _USERS:
                return
            _LOADED.clear()
            if _GPU:
                free_cuda()
                _GPU.pop().release()

    def ask(self, system: str, prompt: str, image: VisionImage, schema: dict | None = None,
            max_tokens: int = 1024) -> Reply:
        """The model's answer. It has no constrained decoding: `schema` is for the prompt to
        describe, and the caller parses the text."""
        # convert_many shares one GPU model across documents. Loading and generation must
        # hold the same lock to avoid duplicate model copies or simultaneous allocations.
        with _MODEL_LOCK:
            return self._generate(image.data, system, prompt, max_tokens)

    def _generate(self, image: bytes, system: str, prompt: str, max_tokens: int) -> Reply:
        import torch
        from PIL import Image
        try:
            model, processor = self.load()
        except Exception as e:
            hint = (" (too little memory for this model on the CPU; use a GPU)"
                    if self.device == "cpu" and isinstance(e, MemoryError | RuntimeError) else "")
            raise ProviderUnavailable(f"could not load {self.model_id}: {e}{hint}") from e
        picture = Image.open(io.BytesIO(image)).convert("RGB")
        # A wide crop (a page-width banner) becomes thousands of image tokens; past this size
        # the model reads no more detail, only uses more GPU memory.
        picture.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
        conversation = [
            {"role": "system", "content": [{"type": "text", "text": system}]},
            {"role": "user", "content": [
                {"type": "image", "image": picture},
                {"type": "text", "text": prompt},
            ]},
        ]
        inputs = None
        try:
            inputs = processor.apply_chat_template(
                conversation, add_generation_prompt=True, tokenize=True, return_dict=True,
                return_tensors="pt").to(self.device)
            with torch.inference_mode():
                output = model.generate(**inputs, max_new_tokens=max_tokens, do_sample=False)
        except (MemoryError, RuntimeError) as e:
            # One figure too big for what is free: it keeps its placeholder, the rest go on.
            # CUDA raises OutOfMemoryError (a RuntimeError); CPU and MPS raise a plain
            # RuntimeError or MemoryError. Any other RuntimeError is a real bug and propagates.
            if not _is_oom(e):
                raise
            del inputs
            if self.device == "cuda":
                torch.cuda.empty_cache()
            raise ProviderError(f"out of memory on {self.device} for this image") from e
        tokens_in = inputs["input_ids"].shape[1]
        generated = output[0][tokens_in:]
        text = processor.decode(generated, skip_special_tokens=True)
        eos = model.generation_config.eos_token_id
        eos_ids = set(eos) if isinstance(eos, list) else {eos}
        truncated = len(generated) >= max_tokens and int(generated[-1]) not in eos_ids
        return Reply(text, truncated, Usage(int(tokens_in), len(generated)))


def _is_oom(error: BaseException) -> bool:
    if isinstance(error, MemoryError):
        return True
    text = str(error).lower()
    return any(m in text for m in ("out of memory", "can't allocate memory", "cannot allocate memory"))
