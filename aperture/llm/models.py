"""
Model discovery and GGUF metadata.

Models are found by scanning a directory for ``.gguf`` files.  That much is
trivial; the interesting part is reading enough of each file's header to tell
the operator what they are about to load.

A GGUF file begins with a key/value metadata block, so the architecture, the
parameter count, the quantisation and -- most usefully -- the context length
the model was actually trained for are all available without loading a single
tensor.  Knowing the trained context length matters because the context
setting is adjustable: raising it past what the model was trained on produces
a model that runs and quietly degrades, which is far worse than one that
refuses.  The settings screen warns instead of letting that happen silently.

The reader is deliberately bounded.  It stops at the end of the metadata block,
skips over array values (a tokenizer vocabulary is hundreds of thousands of
strings and there is no reason to materialise it), and gives up rather than
raising if it meets something it does not understand.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

GGUF_MAGIC = b"GGUF"

# GGUF metadata value types.
(_U8, _I8, _U16, _I16, _U32, _I32, _F32, _BOOL,
 _STRING, _ARRAY, _U64, _I64, _F64) = range(13)

_FIXED = {
    _U8: ("<B", 1), _I8: ("<b", 1),
    _U16: ("<H", 2), _I16: ("<h", 2),
    _U32: ("<I", 4), _I32: ("<i", 4),
    _F32: ("<f", 4), _BOOL: ("<?", 1),
    _U64: ("<Q", 8), _I64: ("<q", 8), _F64: ("<d", 8),
}

#: ``general.file_type`` values worth naming.  The list is not exhaustive --
#: llama.cpp adds types regularly -- so unknown values fall back to the number.
_FILE_TYPES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0",
    9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS",
    24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
    29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16", 36: "TQ1_0",
    37: "TQ2_0",
}

#: Metadata keys worth keeping.  Everything else is skipped without decoding.
_WANTED_SUFFIXES = (
    "context_length", "block_count", "embedding_length",
    "attention.head_count", "rope.freq_base", "expert_count",
)
_WANTED_EXACT = {
    "general.architecture", "general.name", "general.file_type",
    "general.size_label", "general.basename", "general.quantization_version",
}


@dataclass
class ModelInfo:
    """A GGUF file on disk, with whatever metadata could be read from it."""

    path: str
    size_bytes: int = 0
    architecture: str = ""
    name: str = ""
    size_label: str = ""
    quantisation: str = ""
    train_context: int = 0
    block_count: int = 0
    embedding_length: int = 0
    expert_count: int = 0
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return os.path.basename(self.path)

    @property
    def stem(self) -> str:
        return os.path.splitext(self.filename)[0]

    @property
    def size_gib(self) -> float:
        return self.size_bytes / (1024 ** 3)

    def short_name(self, width: int = 18) -> str:
        """A name that fits the panel, preferring the informative end.

        Model filenames are back-loaded -- the distinguishing part is the
        parameter count and quantisation at the end -- so when a name has to be
        cut, the head is what goes.
        """
        name = self.stem
        if len(name) <= width:
            return name
        return "\x7f" + name[-(width - 1):]

    def summary(self) -> str:
        bits = []
        if self.size_label:
            bits.append(self.size_label)
        if self.quantisation:
            bits.append(self.quantisation)
        bits.append(f"{self.size_gib:.1f}G")
        return " ".join(bits)

    def describe_lines(self) -> List[str]:
        """Detail rows for the model inspector."""
        lines = [f"FILE {self.stem}"]
        if self.error:
            lines.append(f"HEADER {self.error}")
        if self.architecture:
            lines.append(f"ARCH {self.architecture}")
        if self.size_label:
            lines.append(f"PARAMS {self.size_label}")
        if self.quantisation:
            lines.append(f"QUANT {self.quantisation}")
        lines.append(f"DISK {self.size_gib:.2f} GiB")
        if self.train_context:
            lines.append(f"TRAINED CTX {self.train_context}")
        if self.block_count:
            lines.append(f"LAYERS {self.block_count}")
        if self.expert_count:
            lines.append(f"EXPERTS {self.expert_count}")
        return lines


def _read(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise EOFError("truncated GGUF header")
    return data


def _read_value(handle, vtype: int, skip: bool) -> Any:
    if vtype in _FIXED:
        fmt, size = _FIXED[vtype]
        raw = _read(handle, size)
        return None if skip else struct.unpack(fmt, raw)[0]
    if vtype == _STRING:
        length = struct.unpack("<Q", _read(handle, 8))[0]
        if length > 1 << 22:
            raise ValueError("implausible GGUF string length")
        raw = _read(handle, length)
        return None if skip else raw.decode("utf-8", errors="replace")
    if vtype == _ARRAY:
        item_type = struct.unpack("<I", _read(handle, 4))[0]
        count = struct.unpack("<Q", _read(handle, 8))[0]
        if item_type in _FIXED:
            # Fixed-width elements can be skipped with one seek.
            handle.seek(_FIXED[item_type][1] * count, os.SEEK_CUR)
            return None
        if item_type == _STRING:
            for _ in range(count):
                length = struct.unpack("<Q", _read(handle, 8))[0]
                handle.seek(length, os.SEEK_CUR)
            return None
        raise ValueError(f"unsupported GGUF array element type {item_type}")
    raise ValueError(f"unsupported GGUF value type {vtype}")


def read_gguf_metadata(path: str, max_pairs: int = 4096) -> Dict[str, Any]:
    """Read the metadata block of a GGUF file.  Never raises."""
    out: Dict[str, Any] = {}
    try:
        with open(path, "rb") as handle:
            if _read(handle, 4) != GGUF_MAGIC:
                out["_error"] = "not a GGUF file"
                return out
            version = struct.unpack("<I", _read(handle, 4))[0]
            out["_version"] = version
            if version < 2 or version > 3:
                out["_error"] = f"unsupported GGUF version {version}"
                return out
            _tensor_count = struct.unpack("<Q", _read(handle, 8))[0]
            kv_count = struct.unpack("<Q", _read(handle, 8))[0]
            if kv_count > max_pairs:
                kv_count = max_pairs

            for _ in range(kv_count):
                key_len = struct.unpack("<Q", _read(handle, 8))[0]
                if key_len > 1024:
                    raise ValueError("implausible GGUF key length")
                key = _read(handle, key_len).decode("utf-8", errors="replace")
                vtype = struct.unpack("<I", _read(handle, 4))[0]
                wanted = key in _WANTED_EXACT or key.endswith(_WANTED_SUFFIXES)
                value = _read_value(handle, vtype, skip=not wanted)
                if wanted and value is not None:
                    out[key] = value
    except (OSError, EOFError, ValueError, struct.error) as exc:
        out["_error"] = str(exc)
    return out


def inspect_model(path: str) -> ModelInfo:
    """Build a :class:`ModelInfo` for one GGUF file."""
    info = ModelInfo(path=path)
    try:
        info.size_bytes = os.path.getsize(path)
    except OSError:
        pass

    meta = read_gguf_metadata(path)
    info.metadata = meta
    info.error = meta.get("_error", "")
    info.architecture = meta.get("general.architecture", "") or ""
    info.name = meta.get("general.name", "") or ""
    info.size_label = meta.get("general.size_label", "") or ""

    file_type = meta.get("general.file_type")
    if isinstance(file_type, int):
        info.quantisation = _FILE_TYPES.get(file_type, f"TYPE{file_type}")
    else:
        info.quantisation = _guess_quant_from_name(info.stem)

    arch = info.architecture
    if arch:
        info.train_context = int(meta.get(f"{arch}.context_length", 0) or 0)
        info.block_count = int(meta.get(f"{arch}.block_count", 0) or 0)
        info.embedding_length = int(meta.get(f"{arch}.embedding_length", 0) or 0)
        info.expert_count = int(meta.get(f"{arch}.expert_count", 0) or 0)
    if not info.size_label:
        info.size_label = _guess_params_from_name(info.stem)
    return info


_QUANT_TOKENS = ("Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L", "Q4_K_S", "Q4_K_M",
                 "Q5_K_S", "Q5_K_M", "Q6_K", "Q8_0", "Q4_0", "Q5_0", "Q5_1",
                 "Q4_1", "IQ4_XS", "IQ4_NL", "IQ3_M", "IQ3_S", "IQ2_M",
                 "IQ2_S", "IQ1_M", "IQ1_S", "BF16", "F16", "F32")


def _guess_quant_from_name(stem: str) -> str:
    upper = stem.upper()
    for token in _QUANT_TOKENS:
        if token in upper:
            return token
    return ""


def _guess_params_from_name(stem: str) -> str:
    import re
    match = re.search(r"(\d+(?:\.\d+)?)\s*[Bb](?![A-Za-z])", stem)
    return f"{match.group(1)}B" if match else ""


def scan_models(directory: str, deep: bool = True) -> List[ModelInfo]:
    """Find every GGUF model under *directory*, newest-looking first.

    Multi-part models (``...-00001-of-00003.gguf``) are represented by their
    first shard only: that is the file llama.cpp is given, and it pulls in the
    rest itself.  Listing all shards would just be a menu full of decoys.
    """
    results: List[ModelInfo] = []
    if not directory or not os.path.isdir(directory):
        return results

    paths: List[str] = []
    if deep:
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.lower().endswith(".gguf"):
                    paths.append(os.path.join(root, name))
    else:
        for name in sorted(os.listdir(directory)):
            if name.lower().endswith(".gguf"):
                paths.append(os.path.join(directory, name))

    import re
    shard = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.IGNORECASE)
    keep = []
    for path in paths:
        match = shard.search(path)
        if match and match.group(1) != "00001":
            continue
        keep.append(path)

    for path in sorted(keep):
        results.append(inspect_model(path))
    return results


def resolve_model(directory: str, preferred: str) -> Optional[ModelInfo]:
    """Pick the configured model, or fall back to the first one present.

    *preferred* may be a bare filename, a path relative to the models
    directory, or an absolute path -- all three are things a person editing the
    config by hand would reasonably write.
    """
    models = scan_models(directory)
    if preferred:
        expanded = os.path.expanduser(preferred)
        candidates = [expanded, os.path.join(directory, preferred)]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return inspect_model(candidate)
        for model in models:
            if preferred in (model.filename, model.stem):
                return model
    return models[0] if models else None
