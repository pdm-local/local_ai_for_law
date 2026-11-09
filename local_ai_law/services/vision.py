"""
Vision pipeline  -  local Ollama multimodal model.

Flow (single):
  1. analyze_image(image_b64, query, model)  →  VisionResult
  2. VisionResult.is_code  →  route to coder agent
  3. VisionResult.description  →  injected as extra context into the main LLM

Flow (multi):
  analyze_images_sequential(images, query, model)  →  list[VisionResult]
  Processes one image at a time (Ollama occupies one GPU slot; parallel calls
  just queue and waste VRAM).  The caller should emit a vision_result WS event
  after each result so the UI can show progressive badges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from local_ai_law.config import log, ollama_keep_alive, safe_num_ctx
from local_ai_law.services.ollama_client import chat as ollama_chat


# ── Code-detection heuristic applied to vision model output ──────────────────

_CODE_SIGNALS = re.compile(
    r'\b(code|function|class|method|variable|import|def |return |for |while |if |else |'
    r'script|algorithm|pseudocode|syntax|python|javascript|java|c\+\+|bash|sql|html|css|'
    r'snippet|loop|error|exception|stack trace|traceback|output|terminal|console)\b',
    re.IGNORECASE,
)


def _is_code_image(description: str) -> bool:
    """Heuristic: does the vision description suggest the image is code or a terminal?"""
    hits = len(_CODE_SIGNALS.findall(description))
    return hits >= 3


# ── Vision prompt templates ───────────────────────────────────────────────────

_ANALYZE_PROMPT = """\
You are a multimodal assistant specialized in understanding images from academic and technical contexts.

Analyze the provided image carefully. Your response must:
1. Describe what the image shows (graph, chart, diagram, code, table, text, etc.)
2. Extract all key information — axis labels, values, trends, code content, equations, etc.
3. Note any important relationships, patterns, or anomalies visible
4. If it is a code screenshot: reproduce the code exactly as text

Be thorough but concise. The user's question is: {query}
Focus your analysis on what is most relevant to answering that question."""


@dataclass
class VisionResult:
    description: str       # full analysis from vision model
    is_code: bool          # should this route to the coder agent?
    image_type: str        # "code" | "graph" | "chart" | "diagram" | "text" | "other"
    model_used: str


_MAX_IMAGE_PX = 2048  # longest side — raised from 1280 to preserve text/code readability


def _resize_image_b64(image_b64: str) -> str:
    """Downscale to _MAX_IMAGE_PX on the longest side to keep Ollama fast."""
    try:
        import base64, io
        from PIL import Image
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        if max(w, h) > _MAX_IMAGE_PX:
            scale = _MAX_IMAGE_PX / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.warning("Image resize failed: %s", e)
        try:
            import base64 as _b64, io as _io
            from PIL import Image as _Image
            raw = _b64.b64decode(image_b64)
            img = _Image.open(_io.BytesIO(raw))
            if max(img.size) > _MAX_IMAGE_PX * 2:  # > 2560px: unsafe to send unresized
                raise RuntimeError(
                    f"Image too large ({img.size[0]}×{img.size[1]}) and resize failed"
                ) from e
        except RuntimeError:
            raise
        except Exception:
            # can't check size — send as-is and let Ollama decide
            log.debug("Image size check failed; sending image unmodified", exc_info=True)
        return image_b64


def analyze_image(
    image_b64: str,
    query: str,
    vision_model: str,
    num_ctx: int = 4096,
) -> VisionResult:
    """
    Call the local vision model to analyze an image.
    Returns a VisionResult with the description and routing hint.
    """
    # Strip data-URL prefix if present
    if "," in image_b64 and image_b64.startswith("data:"):
        image_b64 = image_b64.split(",", 1)[1]

    image_b64 = _resize_image_b64(image_b64)
    num_ctx = safe_num_ctx(num_ctx, 2048)

    try:
        resp = ollama_chat(
            model=vision_model,
            messages=[{
                "role": "user",
                "content": _ANALYZE_PROMPT.format(query=query),
                "images": [image_b64],
            }],
            stream=False,
            num_ctx=num_ctx,
            keep_alive=ollama_keep_alive(),
        )
        description = resp["message"]["content"].strip()
    except Exception as e:
        log.error("Vision model error (%s): %s", vision_model, e)
        raise RuntimeError(f"Vision model '{vision_model}' failed: {e}") from e

    # Classify image type
    is_code = _is_code_image(description)
    if is_code:
        image_type = "code"
    elif any(w in description.lower() for w in ("graph", "plot", "curve", "axis", "y-axis", "x-axis")):
        image_type = "graph"
    elif any(w in description.lower() for w in ("chart", "bar", "pie", "histogram")):
        image_type = "chart"
    elif any(w in description.lower() for w in ("diagram", "flow", "uml", "architecture")):
        image_type = "diagram"
    elif any(w in description.lower() for w in ("table", "column", "row", "cell")):
        image_type = "table"
    else:
        image_type = "other"

    log.info("Vision analysis done: type=%s, code=%s, model=%s", image_type, is_code, vision_model)
    return VisionResult(
        description=description,
        is_code=is_code,
        image_type=image_type,
        model_used=vision_model,
    )


def analyze_images_sequential(
    images: list[str],
    query: str,
    vision_model: str,
    num_ctx: int = 4096,
) -> list[VisionResult]:
    """Analyze multiple images one at a time and return ordered results."""
    results: list[VisionResult] = []
    for img_b64 in images:
        results.append(analyze_image(img_b64, query, vision_model, num_ctx))
    return results
