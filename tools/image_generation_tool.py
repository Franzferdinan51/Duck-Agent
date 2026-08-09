"""
Image Generation Tools Module

Provides image generation via FAL.ai. Multiple FAL models are supported and
selectable via ``duck-agent tools`` → Image Generation; the active model is
persisted to ``image_gen.model`` in ``config.yaml``.

Architecture:
- ``FAL_MODELS`` is a catalog of supported models with per-model metadata
  (size-style family, defaults, ``supports`` whitelist, upscaler flag).
- ``_build_fal_payload()`` translates the agent's unified inputs (prompt +
  aspect_ratio) into the model-specific payload and filters to the
  ``supports`` whitelist so models never receive rejected keys.
- Upscaling via FAL's Clarity Upscaler is gated per-model via the ``upscale``
  flag — on for FLUX 2 Pro (backward-compat), off for all faster/newer models
  where upscaling would either hurt latency or add marginal quality.

Pricing shown in UI strings is as-of the initial commit; we accept drift and
update when it's noticed.
"""
import json
import logging
import os
import datetime
import threading
import uuid
from typing import Any, Dict, Optional
fal_client: Any = None

def _load_fal_client() -> Any:
    """Lazily import fal_client and rebind the module global on first use.

    Idempotent. Returns the (now-loaded) ``fal_client`` module reference.
    Skips the import if the global is already truthy — this preserves the
    test pattern of monkeypatching the module global to install a mock.
    """
    global fal_client
    if fal_client is not None:
        return fal_client
    from tools.fal_common import import_fal_client
    fal_client = import_fal_client()
    return fal_client
from tools.debug_helpers import DebugSession
from tools.fal_common import _ManagedFalSyncClient, _extract_http_status, _normalize_fal_queue_url_format
from tools.managed_tool_gateway import resolve_managed_tool_gateway
from tools.tool_backend_helpers import fal_key_is_configured, managed_nous_tools_enabled, nous_tool_gateway_unavailable_message, prefers_gateway
logger = logging.getLogger(__name__)
FAL_MODELS: Dict[str, Dict[str, Any]] = {'fal-ai/flux-2/klein/9b': {'display': 'FLUX 2 Klein 9B', 'speed': '<1s', 'strengths': 'Fast, crisp text', 'price': '$0.006/MP', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_16_9', 'square': 'square_hd', 'portrait': 'portrait_16_9'}, 'defaults': {'num_inference_steps': 4, 'output_format': 'png', 'enable_safety_checker': False}, 'supports': {'prompt', 'image_size', 'num_inference_steps', 'seed', 'output_format', 'enable_safety_checker'}, 'upscale': False, 'edit_endpoint': 'fal-ai/flux-2/klein/9b/edit', 'edit_supports': {'prompt', 'image_urls', 'num_inference_steps', 'seed', 'output_format', 'enable_safety_checker'}, 'max_reference_images': 9}, 'fal-ai/flux-2-pro': {'display': 'FLUX 2 Pro', 'speed': '~6s', 'strengths': 'Studio photorealism', 'price': '$0.03/MP', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_16_9', 'square': 'square_hd', 'portrait': 'portrait_16_9'}, 'defaults': {'num_inference_steps': 50, 'guidance_scale': 4.5, 'num_images': 1, 'output_format': 'png', 'enable_safety_checker': False, 'safety_tolerance': '5', 'sync_mode': True}, 'supports': {'prompt', 'image_size', 'num_inference_steps', 'guidance_scale', 'num_images', 'output_format', 'enable_safety_checker', 'safety_tolerance', 'sync_mode', 'seed'}, 'upscale': True, 'edit_endpoint': 'fal-ai/flux-2-pro/edit', 'edit_supports': {'prompt', 'image_urls', 'num_inference_steps', 'guidance_scale', 'num_images', 'output_format', 'enable_safety_checker', 'safety_tolerance', 'sync_mode', 'seed'}, 'max_reference_images': 9}, 'fal-ai/z-image/turbo': {'display': 'Z-Image Turbo', 'speed': '~2s', 'strengths': 'Bilingual EN/CN, 6B', 'price': '$0.005/MP', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_16_9', 'square': 'square_hd', 'portrait': 'portrait_16_9'}, 'defaults': {'num_inference_steps': 8, 'num_images': 1, 'output_format': 'png', 'enable_safety_checker': False, 'enable_prompt_expansion': False}, 'supports': {'prompt', 'image_size', 'num_inference_steps', 'num_images', 'seed', 'output_format', 'enable_safety_checker', 'enable_prompt_expansion'}, 'upscale': False}, 'fal-ai/nano-banana-pro': {'display': 'Nano Banana Pro (Gemini 3 Pro Image)', 'speed': '~8s', 'strengths': 'Gemini 3 Pro, reasoning depth, text rendering', 'price': '$0.15/image (1K)', 'size_style': 'aspect_ratio', 'sizes': {'landscape': '16:9', 'square': '1:1', 'portrait': '9:16'}, 'defaults': {'num_images': 1, 'output_format': 'png', 'safety_tolerance': '5', 'resolution': '1K'}, 'supports': {'prompt', 'aspect_ratio', 'num_images', 'output_format', 'safety_tolerance', 'seed', 'sync_mode', 'resolution', 'enable_web_search', 'limit_generations'}, 'upscale': False, 'edit_endpoint': 'fal-ai/nano-banana-pro/edit', 'edit_supports': {'prompt', 'image_urls', 'aspect_ratio', 'num_images', 'output_format', 'safety_tolerance', 'seed', 'sync_mode', 'resolution', 'enable_web_search', 'limit_generations'}, 'max_reference_images': 2}, 'fal-ai/gpt-image-1.5': {'display': 'GPT Image 1.5', 'speed': '~15s', 'strengths': 'Prompt adherence', 'price': '$0.034/image', 'size_style': 'gpt_literal', 'sizes': {'landscape': '1536x1024', 'square': '1024x1024', 'portrait': '1024x1536'}, 'defaults': {'quality': 'medium', 'num_images': 1, 'output_format': 'png'}, 'supports': {'prompt', 'image_size', 'quality', 'num_images', 'output_format', 'background', 'sync_mode'}, 'upscale': False, 'edit_endpoint': 'fal-ai/gpt-image-1.5/edit', 'edit_supports': {'prompt', 'image_urls', 'image_size', 'quality', 'num_images', 'output_format', 'sync_mode'}, 'max_reference_images': 16}, 'fal-ai/gpt-image-2': {'display': 'GPT Image 2', 'speed': '~20s', 'strengths': 'SOTA text rendering + CJK, world-aware photorealism', 'price': '$0.04–0.06/image', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_4_3', 'square': 'square_hd', 'portrait': 'portrait_4_3'}, 'defaults': {'quality': 'medium', 'num_images': 1, 'output_format': 'png'}, 'supports': {'prompt', 'image_size', 'quality', 'num_images', 'output_format', 'sync_mode'}, 'upscale': False, 'edit_endpoint': 'openai/gpt-image-2/edit', 'edit_supports': {'prompt', 'image_urls', 'quality', 'num_images', 'output_format', 'sync_mode', 'mask_image_url'}, 'max_reference_images': 16}, 'fal-ai/ideogram/v3': {'display': 'Ideogram V3', 'speed': '~5s', 'strengths': 'Best typography', 'price': '$0.03-0.09/image', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_16_9', 'square': 'square_hd', 'portrait': 'portrait_16_9'}, 'defaults': {'rendering_speed': 'BALANCED', 'expand_prompt': True, 'style': 'AUTO'}, 'supports': {'prompt', 'image_size', 'rendering_speed', 'expand_prompt', 'style', 'seed'}, 'upscale': False, 'edit_endpoint': 'fal-ai/ideogram/v3/edit', 'edit_supports': {'prompt', 'image_urls', 'rendering_speed', 'expand_prompt', 'style', 'seed'}, 'max_reference_images': 1}, 'fal-ai/recraft/v4/pro/text-to-image': {'display': 'Recraft V4 Pro', 'speed': '~8s', 'strengths': 'Design, brand systems, production-ready', 'price': '$0.25/image', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_16_9', 'square': 'square_hd', 'portrait': 'portrait_16_9'}, 'defaults': {'enable_safety_checker': False}, 'supports': {'prompt', 'image_size', 'enable_safety_checker', 'colors', 'background_color'}, 'upscale': False}, 'fal-ai/qwen-image': {'display': 'Qwen Image', 'speed': '~12s', 'strengths': 'LLM-based, complex text', 'price': '$0.02/MP', 'size_style': 'image_size_preset', 'sizes': {'landscape': 'landscape_16_9', 'square': 'square_hd', 'portrait': 'portrait_16_9'}, 'defaults': {'num_inference_steps': 30, 'guidance_scale': 2.5, 'num_images': 1, 'output_format': 'png', 'acceleration': 'regular'}, 'supports': {'prompt', 'image_size', 'num_inference_steps', 'guidance_scale', 'num_images', 'output_format', 'acceleration', 'seed', 'sync_mode'}, 'upscale': False, 'edit_endpoint': 'fal-ai/qwen-image-2/pro/edit', 'edit_supports': {'prompt', 'image_urls', 'num_inference_steps', 'guidance_scale', 'num_images', 'output_format', 'acceleration', 'seed', 'sync_mode'}, 'max_reference_images': 3}, 'fal-ai/krea/v2/medium/text-to-image': {'display': 'Krea 2 Medium', 'speed': '~15-25s', 'strengths': 'Illustration, anime, painting, expressive/artistic styles', 'price': '$0.030 (text) / $0.035 (style refs)', 'size_style': 'aspect_ratio', 'sizes': {'landscape': '16:9', 'square': '1:1', 'portrait': '9:16'}, 'defaults': {'creativity': 'medium'}, 'supports': {'prompt', 'aspect_ratio', 'creativity', 'seed', 'image_style_references'}, 'upscale': False}, 'fal-ai/krea/v2/large/text-to-image': {'display': 'Krea 2 Large', 'speed': '~25-60s', 'strengths': 'Photorealism, raw textured looks (motion blur, grain, film)', 'price': '$0.060 (text) / $0.065 (style refs)', 'size_style': 'aspect_ratio', 'sizes': {'landscape': '16:9', 'square': '1:1', 'portrait': '9:16'}, 'defaults': {'creativity': 'medium'}, 'supports': {'prompt', 'aspect_ratio', 'creativity', 'seed', 'image_style_references'}, 'upscale': False}}
DEFAULT_MODEL = 'fal-ai/flux-2/klein/9b'
DEFAULT_ASPECT_RATIO = 'landscape'
VALID_ASPECT_RATIOS = ('landscape', 'square', 'portrait')
UPSCALER_MODEL = 'fal-ai/clarity-upscaler'
UPSCALER_FACTOR = 2
UPSCALER_SAFETY_CHECKER = False
UPSCALER_DEFAULT_PROMPT = 'masterpiece, best quality, highres'
UPSCALER_NEGATIVE_PROMPT = '(worst quality, low quality, normal quality:2)'
UPSCALER_CREATIVITY = 0.35
UPSCALER_RESEMBLANCE = 0.6
UPSCALER_GUIDANCE_SCALE = 4
UPSCALER_NUM_INFERENCE_STEPS = 18
_debug = DebugSession('image_tools', env_var='IMAGE_TOOLS_DEBUG')
_managed_fal_client = None
_managed_fal_client_config = None
_managed_fal_client_lock = threading.Lock()

def _resolve_managed_fal_gateway():
    """Return managed fal-queue gateway config when the user prefers the gateway
    or direct FAL credentials are absent."""
    if fal_key_is_configured() and (not prefers_gateway('image_gen')):
        return None
    return resolve_managed_tool_gateway('fal-queue')

def _get_managed_fal_client(managed_gateway):
    """Reuse the managed FAL client so its internal httpx.Client is not leaked per call."""
    global _managed_fal_client, _managed_fal_client_config
    client_config = (managed_gateway.gateway_origin.rstrip('/'), managed_gateway.nous_user_token)
    with _managed_fal_client_lock:
        if _managed_fal_client is not None and _managed_fal_client_config == client_config:
            return _managed_fal_client
        _load_fal_client()
        _managed_fal_client = _ManagedFalSyncClient(fal_client, key=managed_gateway.nous_user_token, queue_run_origin=managed_gateway.gateway_origin)
        _managed_fal_client_config = client_config
        return _managed_fal_client

def _submit_fal_request(model: str, arguments: Dict[str, Any]):
    """Submit a FAL request using direct credentials or the managed queue gateway."""
    _load_fal_client()
    request_headers = {'x-idempotency-key': str(uuid.uuid4())}
    managed_gateway = _resolve_managed_fal_gateway()
    if managed_gateway is None:
        return fal_client.submit(model, arguments=arguments, headers=request_headers)
    managed_client = _get_managed_fal_client(managed_gateway)
    try:
        return managed_client.submit(model, arguments=arguments, headers=request_headers)
    except Exception as exc:
        status = _extract_http_status(exc)
        if status is not None and 400 <= status < 500:
            gateway_message = ''
            if status in {401, 402, 403}:
                gateway_message = '\n\n' + nous_tool_gateway_unavailable_message('managed FAL image generation', force_fresh=True)
            raise ValueError(f"Nous Subscription gateway rejected model '{model}' (HTTP {status}). This model may not yet be enabled on the Nous Portal's FAL proxy. Either:\n  • Set FAL_KEY in your environment to use FAL.ai directly, or\n  • Pick a different model via `duck-agent tools` → Image Generation.{gateway_message}") from exc
        raise

def _resolve_fal_model() -> tuple:
    """Resolve the active FAL model from config.yaml (primary) or default.

    Returns (model_id, metadata_dict). Falls back to DEFAULT_MODEL if the
    configured model is unknown (logged as a warning).
    """
    model_id = ''
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        img_cfg = cfg.get('image_gen') if isinstance(cfg, dict) else None
        if isinstance(img_cfg, dict):
            raw = img_cfg.get('model')
            if isinstance(raw, str):
                model_id = raw.strip()
    except Exception as exc:
        logger.debug('Could not load image_gen.model from config: %s', exc)
    if not model_id:
        model_id = os.getenv('FAL_IMAGE_MODEL', '').strip()
    if not model_id:
        return (DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL])
    if model_id not in FAL_MODELS:
        logger.warning("Unknown FAL model '%s' in config; falling back to %s", model_id, DEFAULT_MODEL)
        return (DEFAULT_MODEL, FAL_MODELS[DEFAULT_MODEL])
    return (model_id, FAL_MODELS[model_id])

def _build_fal_payload(model_id: str, prompt: str, aspect_ratio: str=DEFAULT_ASPECT_RATIO, seed: Optional[int]=None, overrides: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Build a FAL request payload for `model_id` from unified inputs.

    Translates aspect_ratio into the model's native size spec (preset enum,
    aspect-ratio enum, or GPT literal string), merges model defaults, applies
    caller overrides, then filters to the model's ``supports`` whitelist.
    """
    meta = FAL_MODELS[model_id]
    size_style = meta['size_style']
    sizes = meta['sizes']
    aspect = (aspect_ratio or DEFAULT_ASPECT_RATIO).lower().strip()
    if aspect not in sizes:
        aspect = DEFAULT_ASPECT_RATIO
    payload: Dict[str, Any] = dict(meta.get('defaults', {}))
    payload['prompt'] = (prompt or '').strip()
    if size_style in {'image_size_preset', 'gpt_literal'}:
        payload['image_size'] = sizes[aspect]
    elif size_style == 'aspect_ratio':
        payload['aspect_ratio'] = sizes[aspect]
    else:
        raise ValueError(f'Unknown size_style: {size_style!r}')
    if seed is not None and isinstance(seed, int):
        payload['seed'] = seed
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                payload[k] = v
    supports = meta['supports']
    return {k: v for k, v in payload.items() if k in supports or k == 'prompt'}

def _build_fal_edit_payload(model_id: str, prompt: str, image_urls: list, aspect_ratio: str=DEFAULT_ASPECT_RATIO, seed: Optional[int]=None, overrides: Optional[Dict[str, Any]]=None) -> Dict[str, Any]:
    """Build a FAL *edit* request payload (image-to-image) from unified inputs.

    Every FAL edit endpoint takes ``image_urls`` (a list of source/reference
    image URLs) plus the prompt. Size handling differs from text-to-image:
    most edit endpoints auto-infer output dimensions from the input image, so
    we only send ``image_size`` / ``aspect_ratio`` when the edit endpoint's
    ``edit_supports`` whitelist accepts it. Keys outside ``edit_supports`` are
    stripped before submission.
    """
    meta = FAL_MODELS[model_id]
    edit_supports = meta.get('edit_supports') or set()
    size_style = meta['size_style']
    sizes = meta['sizes']
    aspect = (aspect_ratio or DEFAULT_ASPECT_RATIO).lower().strip()
    if aspect not in sizes:
        aspect = DEFAULT_ASPECT_RATIO
    payload: Dict[str, Any] = dict(meta.get('defaults', {}))
    payload['prompt'] = (prompt or '').strip()
    payload['image_urls'] = list(image_urls)
    if size_style in {'image_size_preset', 'gpt_literal'} and 'image_size' in edit_supports:
        payload['image_size'] = sizes[aspect]
    elif size_style == 'aspect_ratio' and 'aspect_ratio' in edit_supports:
        payload['aspect_ratio'] = sizes[aspect]
    if seed is not None and isinstance(seed, int):
        payload['seed'] = seed
    if overrides:
        for k, v in overrides.items():
            if v is not None:
                payload[k] = v
    _required = {'prompt', 'image_urls'}
    return {k: v for k, v in payload.items() if k in edit_supports or k in _required}

def _upscale_image(image_url: str, original_prompt: str) -> Optional[Dict[str, Any]]:
    """Upscale an image using FAL.ai's Clarity Upscaler.

    Returns upscaled image dict, or None on failure (caller falls back to
    the original image).
    """
    try:
        logger.info('Upscaling image with Clarity Upscaler...')
        upscaler_arguments = {'image_url': image_url, 'prompt': f'{UPSCALER_DEFAULT_PROMPT}, {original_prompt}', 'upscale_factor': UPSCALER_FACTOR, 'negative_prompt': UPSCALER_NEGATIVE_PROMPT, 'creativity': UPSCALER_CREATIVITY, 'resemblance': UPSCALER_RESEMBLANCE, 'guidance_scale': UPSCALER_GUIDANCE_SCALE, 'num_inference_steps': UPSCALER_NUM_INFERENCE_STEPS, 'enable_safety_checker': UPSCALER_SAFETY_CHECKER}
        handler = _submit_fal_request(UPSCALER_MODEL, arguments=upscaler_arguments)
        result = handler.get()
        if result and 'image' in result:
            upscaled_image = result['image']
            logger.info('Image upscaled successfully to %sx%s', upscaled_image.get('width', 'unknown'), upscaled_image.get('height', 'unknown'))
            return {'url': upscaled_image['url'], 'width': upscaled_image.get('width', 0), 'height': upscaled_image.get('height', 0), 'upscaled': True, 'upscale_factor': UPSCALER_FACTOR}
        logger.error('Upscaler returned invalid response')
        return None
    except Exception as e:
        logger.error('Error upscaling image: %s', e, exc_info=True)
        return None

def _looks_like_absolute_file_path(value: str) -> bool:
    if not value or not isinstance(value, str):
        return False
    lower = value.lower()
    if lower.startswith(('http://', 'https://', 'data:')):
        return False
    if os.path.isabs(value):
        return True
    return len(value) >= 3 and value[1] == ':' and (value[2] in {'/', '\\'})

def _active_terminal_env(task_id: str | None):
    try:
        from tools.terminal_tool import get_active_env
        return get_active_env(task_id or 'default')
    except Exception as exc:
        logger.debug('Could not inspect active terminal environment: %s', exc)
        return None

def _agent_cache_base_for_env(env: Any) -> str | None:
    if env is not None:
        explicit = getattr(env, 'agent_visible_cache_base', None)
        if callable(explicit):
            try:
                value = explicit()
                if value:
                    return str(value).rstrip('/')
            except Exception as exc:
                logger.debug('active env agent_visible_cache_base failed: %s', exc)
        remote_home = getattr(env, '_remote_home', None)
        if remote_home:
            return f"{str(remote_home).rstrip('/')}/.duck-agent"
        env_name = env.__class__.__name__
        if env_name in {'DockerEnvironment', 'SingularityEnvironment', 'ModalEnvironment'}:
            return '/root/.duck-agent'
    backend = (os.getenv('TERMINAL_ENV') or 'local').strip().lower()
    if backend in {'docker', 'singularity', 'modal'}:
        return '/root/.duck-agent'
    if backend == 'ssh':
        return '~/.duck-agent'
    return None

def _agent_visible_cache_path(host_path: str, env: Any) -> str | None:
    if not _looks_like_absolute_file_path(host_path):
        return None
    cache_base = _agent_cache_base_for_env(env)
    if not cache_base:
        return None
    try:
        from tools.credential_files import map_cache_path_to_container
        return map_cache_path_to_container(host_path, container_base=cache_base)
    except Exception as exc:
        logger.debug('Could not translate image cache path for backend: %s', exc)
    return None

def _force_artifact_sync(env: Any) -> None:
    sync_manager = getattr(env, '_sync_manager', None)
    if sync_manager is None:
        return
    try:
        sync_manager.sync(force=True)
    except Exception as exc:
        logger.warning('Could not force-sync generated image artifact: %s', exc)

def _postprocess_image_generate_result(raw: str, task_id: str | None=None) -> str:
    """Annotate successful local image results with backend-visible paths.

    ``image`` remains the host/gateway-deliverable path.  When the active
    terminal backend has a different filesystem, ``agent_visible_image`` gives
    the path the agent can use with terminal/file tools.
    """
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return raw
    if not isinstance(payload, dict) or not payload.get('success'):
        return raw
    image = payload.get('image')
    if not isinstance(image, str) or not _looks_like_absolute_file_path(image):
        return raw
    env = _active_terminal_env(task_id)
    agent_path = _agent_visible_cache_path(image, env)
    if not agent_path or agent_path == image:
        return raw
    if env is not None:
        _force_artifact_sync(env)
    payload.setdefault('host_image', image)
    payload.setdefault('agent_visible_image', agent_path)
    return json.dumps(payload, ensure_ascii=False)

def image_generate_tool(prompt: str, aspect_ratio: str=DEFAULT_ASPECT_RATIO, num_inference_steps: Optional[int]=None, guidance_scale: Optional[float]=None, num_images: Optional[int]=None, output_format: Optional[str]=None, seed: Optional[int]=None, image_url: Optional[str]=None, reference_image_urls: Optional[list]=None) -> str:
    """Generate an image from a text prompt, or edit a source image, via FAL.

    Routing: when ``image_url`` (or ``reference_image_urls``) is provided AND
    the configured model declares an ``edit_endpoint``, the call routes to that
    image-to-image / edit endpoint; otherwise it's plain text-to-image.

    The agent-facing schema exposes ``prompt``, ``aspect_ratio``, ``image_url``
    and ``reference_image_urls``; the remaining kwargs are overrides for direct
    Python callers and are filtered per-model via the ``supports`` /
    ``edit_supports`` whitelist (unsupported overrides are silently dropped so
    legacy callers don't break when switching models).

    Returns a JSON string with ``{"success": bool, "image": url | None,
    "modality": "text" | "image", "error": str, "error_type": str}``.
    """
    model_id, meta = _resolve_fal_model()
    source_images: list = []
    if isinstance(image_url, str) and image_url.strip():
        source_images.append(image_url.strip())
    if isinstance(reference_image_urls, (list, tuple)):
        for ref in reference_image_urls:
            if isinstance(ref, str) and ref.strip():
                source_images.append(ref.strip())
    edit_endpoint = meta.get('edit_endpoint')
    use_edit = bool(source_images) and bool(edit_endpoint)
    modality = 'image' if use_edit else 'text'
    debug_call_data = {'model': model_id, 'parameters': {'prompt': prompt, 'aspect_ratio': aspect_ratio, 'num_inference_steps': num_inference_steps, 'guidance_scale': guidance_scale, 'num_images': num_images, 'output_format': output_format, 'seed': seed, 'modality': modality, 'source_images': len(source_images)}, 'error': None, 'success': False, 'images_generated': 0, 'generation_time': 0}
    start_time = datetime.datetime.now()
    try:
        if not prompt or not isinstance(prompt, str) or len(prompt.strip()) == 0:
            raise ValueError('Prompt is required and must be a non-empty string')
        if not (fal_key_is_configured() or _resolve_managed_fal_gateway()):
            raise ValueError(_build_no_backend_setup_message())
        if source_images and (not edit_endpoint):
            raise ValueError(f"Model '{meta.get('display', model_id)}' ({model_id}) is not capable of image-to-image / editing. Provide a text-only prompt (omit image_url), or switch to an edit-capable model via `duck-agent tools` → Image Generation.")
        aspect_lc = (aspect_ratio or DEFAULT_ASPECT_RATIO).lower().strip()
        if aspect_lc not in VALID_ASPECT_RATIOS:
            logger.warning("Invalid aspect_ratio '%s', defaulting to '%s'", aspect_ratio, DEFAULT_ASPECT_RATIO)
            aspect_lc = DEFAULT_ASPECT_RATIO
        overrides: Dict[str, Any] = {}
        if num_inference_steps is not None:
            overrides['num_inference_steps'] = num_inference_steps
        if guidance_scale is not None:
            overrides['guidance_scale'] = guidance_scale
        if num_images is not None:
            overrides['num_images'] = num_images
        if output_format is not None:
            overrides['output_format'] = output_format
        if use_edit:
            max_refs = int(meta.get('max_reference_images') or 1)
            clamped_sources = source_images[:max_refs] if max_refs > 0 else source_images
            arguments = _build_fal_edit_payload(model_id, prompt, clamped_sources, aspect_lc, seed=seed, overrides=overrides)
            endpoint = edit_endpoint
            logger.info('Editing image with %s (%s) — %d source image(s), prompt: %s', meta.get('display', model_id), endpoint, len(clamped_sources), prompt[:80])
        else:
            arguments = _build_fal_payload(model_id, prompt, aspect_lc, seed=seed, overrides=overrides)
            endpoint = model_id
            logger.info('Generating image with %s (%s) — prompt: %s', meta.get('display', model_id), model_id, prompt[:80])
        handler = _submit_fal_request(endpoint, arguments=arguments)
        result = handler.get()
        generation_time = (datetime.datetime.now() - start_time).total_seconds()
        if not result or 'images' not in result:
            raise ValueError('Invalid response from FAL.ai API — no images returned')
        images = result.get('images', [])
        if not images:
            raise ValueError('No images were generated')
        should_upscale = bool(meta.get('upscale', False)) and (not use_edit)
        formatted_images = []
        for img in images:
            if not (isinstance(img, dict) and 'url' in img):
                continue
            original_image = {'url': img['url'], 'width': img.get('width', 0), 'height': img.get('height', 0)}
            if should_upscale:
                upscaled_image = _upscale_image(img['url'], prompt.strip())
                if upscaled_image:
                    formatted_images.append(upscaled_image)
                    continue
                logger.warning('Using original image as fallback (upscale failed)')
            original_image['upscaled'] = False
            formatted_images.append(original_image)
        if not formatted_images:
            raise ValueError('No valid image URLs returned from API')
        upscaled_count = sum((1 for img in formatted_images if img.get('upscaled')))
        logger.info('Generated %s image(s) in %.1fs (%s upscaled) via %s [%s]', len(formatted_images), generation_time, upscaled_count, endpoint, modality)
        response_data = {'success': True, 'image': formatted_images[0]['url'] if formatted_images else None, 'modality': modality}
        debug_call_data['success'] = True
        debug_call_data['images_generated'] = len(formatted_images)
        debug_call_data['generation_time'] = generation_time
        _debug.log_call('image_generate_tool', debug_call_data)
        _debug.save()
        return json.dumps(response_data, indent=2, ensure_ascii=False)
    except Exception as e:
        generation_time = (datetime.datetime.now() - start_time).total_seconds()
        error_msg = f'Error generating image: {str(e)}'
        logger.error('%s', error_msg, exc_info=True)
        response_data = {'success': False, 'image': None, 'error': str(e), 'error_type': type(e).__name__}
        debug_call_data['error'] = error_msg
        debug_call_data['generation_time'] = generation_time
        _debug.log_call('image_generate_tool', debug_call_data)
        _debug.save()
        return json.dumps(response_data, indent=2, ensure_ascii=False)

def check_fal_api_key() -> bool:
    """True if the FAL.ai API key (direct or managed gateway) is available."""
    return bool(fal_key_is_configured() or _resolve_managed_fal_gateway())

def _build_no_backend_setup_message() -> str:
    """Build an actionable error string when no FAL backend is reachable.

    Used by the in-tree FAL path. Mentions:
      - FAL_KEY signup link
      - managed-gateway status (if Nous tools are enabled)
      - plugin alternative pointer (so users on a stale ``image_gen.provider``
        know the registry exists and how to inspect it)
    """
    lines = ['Image generation is unavailable in this environment.', '']
    lines.append('Missing requirements:')
    if managed_nous_tools_enabled():
        lines.append('  - FAL_KEY is not set and the managed FAL gateway is unreachable')
    else:
        lines.append('  - FAL_KEY environment variable is not set')
        gateway_message = nous_tool_gateway_unavailable_message('managed FAL image generation')
        if gateway_message:
            lines.append(f'  - {gateway_message}')
    lines.append('')
    lines.append('To enable image generation, do one of:')
    lines.append('  1. Get a free API key at https://fal.ai and set FAL_KEY=<your-key> (then restart the session)')
    if managed_nous_tools_enabled():
        lines.append('  2. Sign in to a Nous account that has the managed FAL gateway enabled (`duck-agent setup`)')
    lines.append('  3. Configure a different image_gen provider via `duck-agent tools` → Image Generation (run `duck-agent plugins list` to see installed backends)')
    return '\n'.join(lines)

def check_image_generation_requirements() -> bool:
    """True if FAL or the explicitly configured image backend is available."""
    try:
        if check_fal_api_key():
            _load_fal_client()
            return True
    except ImportError:
        pass
    configured = _read_configured_image_provider()
    if not configured or configured == 'fal':
        return False
    try:
        from agent.image_gen_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
        provider = get_provider(configured)
        return bool(provider and provider.is_available())
    except Exception:
        return False
if __name__ == '__main__':
    print('🎨 Image Generation Tools — FAL.ai multi-model support')
    print('=' * 60)
    if not check_fal_api_key():
        print('❌ FAL_KEY environment variable not set')
        print("   Set it via: export FAL_KEY='your-key-here'")
        print('   Get a key: https://fal.ai/')
        raise SystemExit(1)
    print('✅ FAL.ai API key found')
    try:
        import fal_client
        print('✅ fal_client library available')
    except ImportError:
        print('❌ fal_client library not found — pip install fal-client')
        raise SystemExit(1)
    model_id, meta = _resolve_fal_model()
    print(f"🤖 Active model: {meta.get('display', model_id)} ({model_id})")
    print(f"   Speed: {meta.get('speed', '?')}  ·  Price: {meta.get('price', '?')}")
    print(f"   Upscaler: {('on' if meta.get('upscale') else 'off')}")
    print('\nAvailable models:')
    for mid, m in FAL_MODELS.items():
        marker = ' ← active' if mid == model_id else ''
        print(f"  {mid:<32}  {m.get('speed', '?'):<6}  {m.get('price', '?')}{marker}")
    if _debug.active:
        print(f'\n🐛 Debug mode enabled — session {_debug.session_id}')
from tools.registry import registry, tool_error
IMAGE_GENERATE_SCHEMA = {'name': 'image_generate', 'description': 'Generate high-quality images from text prompts (text-to-image), or edit / transform an existing image (image-to-image) when the active model supports it. Pass `image_url` to edit that image; add `reference_image_urls` for style/composition references; omit both for text-to-image. The underlying backend (FAL, OpenAI, xAI, etc.) and model are user-configured and not selectable by the agent. Returns the result in the `image` field — either a URL or an absolute file path. To show it to the user, reference that path/URL in your response using the file-delivery convention for the current platform (your platform guidance describes how files are delivered here). When the active terminal backend has a different filesystem, successful local-file results may also include `agent_visible_image` for follow-up terminal/file operations.', 'parameters': {'type': 'object', 'properties': {'prompt': {'type': 'string', 'description': 'The text prompt describing the desired image (text-to-image) or the edit to apply (image-to-image). Be detailed and descriptive.'}, 'aspect_ratio': {'type': 'string', 'enum': list(VALID_ASPECT_RATIOS), 'description': "The aspect ratio of the generated image. 'landscape' is 16:9 wide, 'portrait' is 16:9 tall, 'square' is 1:1.", 'default': DEFAULT_ASPECT_RATIO}, 'image_url': {'type': 'string', 'description': 'Optional source image to edit/transform (image-to-image). When provided, the active backend routes to its image editing endpoint; when omitted, it generates from text alone. Pass a public URL or an absolute local file path from the conversation. Only honored by models that support editing — the description above indicates whether the active model does.'}, 'reference_image_urls': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Optional list of additional reference image URLs / paths (style, character, or composition references) to guide an image-to-image edit. Supported only by some models and capped per-model; the description above indicates the max.'}}, 'required': ['prompt']}}

def _read_configured_image_model():
    """Return the value of ``image_gen.model`` from config.yaml, or None."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        section = cfg.get('image_gen') if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            value = section.get('model')
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:
        logger.debug('Could not read image_gen.model: %s', exc)
    return None

def _read_configured_image_provider():
    """Return ``image_gen.provider`` from config.yaml, or None.

    We only consult the plugin registry when this is explicitly set — an
    unset value keeps users on the in-tree FAL fallback even when other
    providers happen to be registered (e.g. a user has OPENAI_API_KEY set
    for other features but never asked for OpenAI image gen). ``"fal"``
    explicitly routes through ``plugins/image_gen/fal/`` (which delegates
    back into this module's pipeline via call-time indirection — see
    issue #26241).
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        section = cfg.get('image_gen') if isinstance(cfg, dict) else None
        if isinstance(section, dict):
            value = section.get('provider')
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:
        logger.debug('Could not read image_gen.provider: %s', exc)
    return None

def _dispatch_to_plugin_provider(prompt: str, aspect_ratio: str, image_url: Optional[str]=None, reference_image_urls: Optional[list]=None):
    """Route the call to a plugin-registered provider when one is selected.

    Returns a JSON string on dispatch, or ``None`` to fall through to the
    in-tree FAL fallback in ``image_generate_tool``.

    Dispatch fires when ``image_gen.provider`` is explicitly set — including
    ``"fal"`` itself, which now resolves to the
    ``plugins/image_gen/fal/`` plugin (the plugin re-enters this module's
    pipeline via ``_it`` indirection so behavior is identical to the
    direct call, just routed through the registry).

    ``image_url`` / ``reference_image_urls`` enable image-to-image / editing:
    they are forwarded to the provider's ``generate()`` so the backend can
    route to its edit endpoint.
    """
    configured = _read_configured_image_provider()
    if not configured or configured == 'fal':
        return None
    configured_model = _read_configured_image_model()
    try:
        from agent.image_gen_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
        provider = get_provider(configured)
    except Exception as exc:
        logger.debug('image_gen plugin dispatch skipped: %s', exc)
        return None
    if provider is None:
        try:
            _ensure_plugins_discovered(force=True)
            provider = get_provider(configured)
        except Exception as exc:
            logger.debug('image_gen plugin force-refresh skipped: %s', exc)
    if provider is None:
        return json.dumps({'success': False, 'image': None, 'error': f"image_gen.provider='{configured}' is set but no plugin registered that name. Run `duck-agent plugins list` to see available image gen backends.", 'error_type': 'provider_not_registered'})
    kwargs: Dict[str, Any] = {'prompt': prompt, 'aspect_ratio': aspect_ratio}
    try:
        if configured_model:
            kwargs['model'] = configured_model
        if isinstance(image_url, str) and image_url.strip():
            kwargs['image_url'] = image_url.strip()
        norm_refs = None
        if reference_image_urls is not None:
            from agent.image_gen_provider import normalize_reference_images
            norm_refs = normalize_reference_images(reference_image_urls)
        if norm_refs:
            kwargs['reference_image_urls'] = norm_refs
        result = provider.generate(**kwargs)
    except TypeError as exc:
        if 'image_url' in kwargs or 'reference_image_urls' in kwargs:
            logger.warning("image_gen provider '%s' rejected image-to-image kwargs (signature too narrow): %s", getattr(provider, 'name', '?'), exc)
            return json.dumps({'success': False, 'image': None, 'error': f"Provider '{getattr(provider, 'name', '?')}' does not support image-to-image / editing (its generate() signature is out of date with the image_generate schema). Omit image_url for text-to-image, or pick a backend that supports editing via `duck-agent tools` → Image Generation.", 'error_type': 'modality_unsupported'})
        logger.warning("Image gen provider '%s' raised TypeError: %s", getattr(provider, 'name', '?'), exc)
        return json.dumps({'success': False, 'image': None, 'error': f"Provider '{getattr(provider, 'name', '?')}' error: {exc}", 'error_type': 'provider_exception'})
    except Exception as exc:
        logger.warning("Image gen provider '%s' raised: %s", getattr(provider, 'name', '?'), exc)
        return json.dumps({'success': False, 'image': None, 'error': f"Provider '{getattr(provider, 'name', '?')}' error: {exc}", 'error_type': 'provider_exception'})
    if not isinstance(result, dict):
        return json.dumps({'success': False, 'image': None, 'error': 'Provider returned a non-dict result', 'error_type': 'provider_contract'})
    return json.dumps(result)
_KREA_NATIVE_MODELS = {'krea-2-medium', 'krea-2-large', 'krea-2-medium-turbo'}

def _normalize_krea_model(model_id: Optional[str]) -> Optional[str]:
    """Return the native Krea plugin model id when ``model_id`` is ``krea-2-*``."""
    if not isinstance(model_id, str):
        return None
    candidate = model_id.strip()
    if candidate in _KREA_NATIVE_MODELS:
        return candidate
    return None

def is_krea_model(model_id: Optional[str]) -> bool:
    """True when ``model_id`` is a native Krea plugin id (``krea-2-*``)."""
    return _normalize_krea_model(model_id) is not None

def _maybe_route_managed_krea(prompt: str, aspect_ratio: str, image_url: Optional[str]=None, reference_image_urls: Optional[list]=None) -> Optional[str]:
    """Route a native ``krea-2-*`` model to the managed Krea gateway, in managed mode.

    Returns a JSON result string when handled by the Krea managed gateway, or
    ``None`` to fall through to the normal plugin/FAL pipeline. Fires only when
    all hold:
      - the configured image model is a native ``krea-2-*`` id, AND
      - the user isn't already routed to the Krea plugin via
        ``image_gen.provider`` (that path dispatches normally), AND
      - the managed Krea gateway is resolvable (portal/managed mode).

    Direct/BYO users (no managed gateway) fall through untouched.
    """
    if _read_configured_image_provider() == 'krea':
        return None
    normalized = _normalize_krea_model(_read_configured_image_model())
    if normalized is None:
        return None
    try:
        from plugins.image_gen.krea import _resolve_managed_krea_gateway
        if _resolve_managed_krea_gateway() is None:
            return None
    except Exception as exc:
        logger.debug('Managed Krea routing probe failed: %s', exc)
        return None
    try:
        from agent.image_gen_registry import get_provider
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
        provider = get_provider('krea')
    except Exception as exc:
        logger.debug('Managed Krea routing: provider unavailable: %s', exc)
        return None
    if provider is None:
        return None
    kwargs: Dict[str, Any] = {'prompt': prompt, 'aspect_ratio': aspect_ratio, 'model': normalized}
    try:
        if isinstance(image_url, str) and image_url.strip():
            kwargs['image_url'] = image_url.strip()
        norm_refs = None
        if reference_image_urls is not None:
            from agent.image_gen_provider import normalize_reference_images
            norm_refs = normalize_reference_images(reference_image_urls)
        if norm_refs:
            kwargs['reference_image_urls'] = norm_refs
        result = provider.generate(**kwargs)
    except Exception as exc:
        logger.warning('Managed Krea routing failed: %s', exc)
        return json.dumps({'success': False, 'image': None, 'error': f'Managed Krea generation error: {exc}', 'error_type': 'provider_exception'})
    if not isinstance(result, dict):
        return json.dumps({'success': False, 'image': None, 'error': 'Krea provider returned a non-dict result', 'error_type': 'provider_contract'})
    return json.dumps(result)

def _handle_image_generate(args, **kw):
    prompt = args.get('prompt', '')
    if not prompt:
        return tool_error('prompt is required for image generation')
    aspect_ratio = args.get('aspect_ratio', DEFAULT_ASPECT_RATIO)
    image_url = args.get('image_url')
    reference_image_urls = args.get('reference_image_urls')
    task_id = kw.get('task_id')
    dispatched = _dispatch_to_plugin_provider(prompt, aspect_ratio, image_url=image_url, reference_image_urls=reference_image_urls)
    if dispatched is not None:
        return _postprocess_image_generate_result(dispatched, task_id=task_id)
    krea_routed = _maybe_route_managed_krea(prompt, aspect_ratio, image_url=image_url, reference_image_urls=reference_image_urls)
    if krea_routed is not None:
        return _postprocess_image_generate_result(krea_routed, task_id=task_id)
    raw = image_generate_tool(prompt=prompt, aspect_ratio=aspect_ratio, image_url=image_url, reference_image_urls=reference_image_urls)
    return _postprocess_image_generate_result(raw, task_id=task_id)
_GENERIC_IMAGE_DESCRIPTION = IMAGE_GENERATE_SCHEMA['description']

def _active_image_capabilities() -> Dict[str, Any]:
    """Best-effort: return the active backend/model's image capabilities.

    Resolution order mirrors the runtime dispatch:
    1. If ``image_gen.provider`` is set, ask that plugin provider.
    2. Otherwise inspect the in-tree FAL model catalog for the active model.

    Returns a dict like ``{"modalities": [...], "max_reference_images": N,
    "model": "...", "provider": "..."}``. Never raises.
    """
    info: Dict[str, Any] = {'modalities': ['text'], 'max_reference_images': 0}
    configured_provider = _read_configured_image_provider()
    if configured_provider and configured_provider != 'fal':
        try:
            from agent.image_gen_registry import get_provider
            from hermes_cli.plugins import _ensure_plugins_discovered
            _ensure_plugins_discovered()
            provider = get_provider(configured_provider)
            if provider is not None:
                caps = {}
                try:
                    caps = provider.capabilities() or {}
                except Exception:
                    caps = {}
                info['provider'] = provider.display_name
                info['model'] = _read_configured_image_model() or (provider.default_model() or '')
                if caps.get('modalities'):
                    info['modalities'] = list(caps['modalities'])
                if caps.get('max_reference_images'):
                    info['max_reference_images'] = int(caps['max_reference_images'])
                return info
        except Exception:
            pass
    try:
        model_id, meta = _resolve_fal_model()
        info['provider'] = 'FAL.ai'
        info['model'] = meta.get('display', model_id)
        if meta.get('edit_endpoint'):
            info['modalities'] = ['text', 'image']
            info['max_reference_images'] = int(meta.get('max_reference_images') or 1)
        else:
            info['modalities'] = ['text']
            info['max_reference_images'] = 0
    except Exception:
        pass
    return info

def _build_dynamic_image_schema() -> Dict[str, Any]:
    """Build a description reflecting whether the active model supports editing."""
    parts = [_GENERIC_IMAGE_DESCRIPTION]
    try:
        info = _active_image_capabilities()
    except Exception:
        return {'description': _GENERIC_IMAGE_DESCRIPTION}
    provider = info.get('provider')
    model = info.get('model')
    modalities = set(info.get('modalities') or ['text'])
    line = '\nActive backend'
    if provider:
        line += f': {provider}'
    if model:
        line += f' · model: {model}'
    parts.append(line)
    if 'image' in modalities and 'text' in modalities:
        max_refs = info.get('max_reference_images') or 0
        ref_note = f'; up to {max_refs} reference image(s) via reference_image_urls' if max_refs and max_refs > 1 else ''
        parts.append(f'- supports both text-to-image (omit image_url) and image-to-image / editing (pass image_url){ref_note} — routes automatically')
    elif 'image' in modalities and 'text' not in modalities:
        parts.append('- this model is image-to-image / edit only — image_url is REQUIRED')
    else:
        parts.append('- this model is text-to-image only — it is NOT capable of image-to-image / editing; do not pass image_url or reference_image_urls (they will be rejected). Provide a text-only prompt.')
    return {'description': '\n'.join(parts)}
registry.register(name='image_generate', toolset='image_gen', schema=IMAGE_GENERATE_SCHEMA, handler=_handle_image_generate, check_fn=check_image_generation_requirements, requires_env=[], is_async=False, emoji='🎨', dynamic_schema_overrides=_build_dynamic_image_schema)