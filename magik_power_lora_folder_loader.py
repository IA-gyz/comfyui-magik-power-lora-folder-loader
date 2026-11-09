"""Magik Power Lora Folder Loader.

A standalone ComfyUI node cloned from rgthree-comfy's "Power Lora Loader", ported into the
Magik ecosystem with no dependency on the rgthree package. All helper utilities that the
original imported from rgthree (constants, power_prompt_utils, utils, server.utils_info, log)
are reimplemented locally below.

Unlike the original, this node adds real folder behavior: pick a lora subfolder from the
"lora_folder" combo and the paired JS frontend (magik_power_lora_folder_loader.js) auto-
populates one on/off + strength row per lora file found directly in that folder -- no manual
"add lora" step. Rows are still sent to Python as the same "lora_x": {on, lora, strength}
dicts the original rgthree design used, via the V3 schema's accept_all_inputs, so execute()
itself needed no logic changes for this feature.

This module also registers a small aiohttp route (GET /magik/power_lora_folder_loader/loras)
that the frontend calls to list the loras inside a chosen folder (it also reports which of
them already have trigger words saved, for the info-button badge).

CivitAI info: each row has an info button. Its dialog can fetch a lora's info from Civitai
(matched by the file's SHA-256) and store it in the "<lora>.json" sidecar next to the lora,
in the same {"trainedWords": [{"word": ...}]} shape get_lora_info_file_data() already reads.
Routes: GET  /magik/power_lora_folder_loader/info?lora=<name>
        POST /magik/power_lora_folder_loader/info/fetch   {"lora": "<name>"}
        POST /magik/power_lora_folder_loader/info/clear   {"lora": "<name>"}
Fetch replaces the keys it owns (trainedWords, civitai, sha256); other keys in the sidecar are
left alone, and Clear removes only those keys. Routes are registered once per server run, so
restart ComfyUI (not just refresh) after changing this file. The folder combo's own
options come from the standard define_schema() scan, so it refreshes via ComfyUI's normal
"refresh node definitions" action -- no separate route needed for that part.
"""

import asyncio
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request

import folder_paths
from nodes import LoraLoader

from comfy_api.latest import io

NODE_NAME = "Magik Power Lora Folder Loader"


# ---------------------------------------------------------------------------
# Local reimplementations of rgthree's shared utilities
# ---------------------------------------------------------------------------

def log_node_warn(node_name: str, message: str):
  """Minimal console warning helper, standing in for rgthree's log module."""
  print(f"[{node_name}] WARNING: {message}")


def get_lora_by_filename(file_path, lora_paths=None, log_node=None):
  """Resolves a lora reference (possibly a partial/renamed path) to an actual filename known
  to folder_paths. Tries, in order: exact match, case-insensitive full-path match,
  case-insensitive basename match, then a basename-stem substring match. Returns None (and
  logs a warning) if nothing usable is found.
  """
  lora_paths = lora_paths if lora_paths is not None else folder_paths.get_filename_list("loras")
  log_node = log_node or NODE_NAME

  if file_path in lora_paths:
    return file_path

  lower_file_path = file_path.lower()
  basename = os.path.basename(file_path).lower()

  matches = [
    p for p in lora_paths
    if p.lower() == lower_file_path or os.path.basename(p).lower() == basename
  ]
  if len(matches) == 1:
    return matches[0]
  if len(matches) > 1:
    log_node_warn(
      log_node,
      f'Multiple loras match "{file_path}"; using first match "{matches[0]}".'
    )
    return matches[0]

  stem = os.path.splitext(basename)[0]
  partial_matches = [p for p in lora_paths if stem and stem in os.path.basename(p).lower()]
  if len(partial_matches) == 1:
    return partial_matches[0]
  if len(partial_matches) > 1:
    log_node_warn(
      log_node,
      f'Multiple loras partially match "{file_path}"; using first match "{partial_matches[0]}".'
    )
    return partial_matches[0]

  log_node_warn(log_node, f'Could not find lora "{file_path}" in the loras folder.')
  return None


def get_lora_info_file_data(lora_filename, default=None):
  """Reads an optional sidecar JSON info file for a lora (same basename, .json extension)
  next to the lora file itself, if present. Expects a dict with a "trainedWords" list of
  {"word": str} entries -- this is a simplified, self-contained stand-in for rgthree's
  server-backed info-file system. Wire this into Magik's own CivitAI trigger-word fetcher
  (as used by Magik LoRA Compare) if you want a shared source of trigger words.
  """
  default = default if default is not None else {}
  full_path = folder_paths.get_full_path("loras", lora_filename)
  if not full_path:
    return default
  info_path = os.path.splitext(full_path)[0] + ".json"
  if not os.path.isfile(info_path):
    return default
  try:
    with open(info_path, "r", encoding="utf-8") as f:
      return json.load(f)
  except (OSError, json.JSONDecodeError) as e:
    log_node_warn(NODE_NAME, f'Failed to read lora info file "{info_path}": {e}')
    return default


# ---------------------------------------------------------------------------
# CivitAI info (stored in the <lora>.json sidecar)
# ---------------------------------------------------------------------------

CIVITAI_BY_HASH_URL = "https://civitai.com/api/v1/model-versions/by-hash/{sha256}"
# Where the dialog's "Open on Civitai" link points. Only the browser link uses this; the hash
# lookup above stays on the documented civitai.com API. Change this one constant to switch sites.
CIVITAI_SITE_URL = "https://civitai.red"
CIVITAI_TIMEOUT_SECONDS = 15
# Keys this feature owns inside the sidecar. Fetch replaces them; Clear removes them. Any other
# key already in the file (from you or another tool) is left untouched.
SIDECAR_OWNED_KEYS = ("trainedWords", "civitai", "sha256")

_hash_cache = {}  # (full_path, mtime_ns, size) -> sha256 hex digest


def resolve_lora_path(lora_name):
  """Maps a lora name to its full path, or None. Only names that appear in folder_paths' own
  lora list are accepted, so a request can never point outside the registered loras dirs."""
  if not isinstance(lora_name, str) or not lora_name:
    return None
  wanted = lora_name.replace(os.sep, "/")
  known = {p.replace(os.sep, "/") for p in folder_paths.get_filename_list("loras")}
  if wanted not in known:
    return None
  return folder_paths.get_full_path("loras", lora_name)


def sidecar_path_for(full_path):
  return os.path.splitext(full_path)[0] + ".json"


def read_sidecar(full_path):
  """Returns (data, error). A missing file gives ({}, None). An unreadable or non-object file
  gives ({}, message) so callers never overwrite something they couldn't parse."""
  path = sidecar_path_for(full_path)
  if not os.path.isfile(path):
    return {}, None
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
  except (OSError, json.JSONDecodeError) as e:
    return {}, f'Could not read "{os.path.basename(path)}": {e}'
  if not isinstance(data, dict):
    return {}, f'"{os.path.basename(path)}" is not a JSON object.'
  return data, None


def write_sidecar(full_path, data):
  """Atomic write (temp file + replace) so a crash can't leave a half-written sidecar."""
  path = sidecar_path_for(full_path)
  fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2, ensure_ascii=False)
    try:
      os.chmod(tmp, 0o644)  # mkstemp creates 0600; keep the sidecar readable like the lora
    except OSError:
      pass
    os.replace(tmp, path)
  except BaseException:
    if os.path.exists(tmp):
      os.remove(tmp)
    raise


def trained_words_from_info(info):
  """Flattens a sidecar's trainedWords ({"word": str} entries or plain strings) to strings."""
  words = []
  for item in info.get("trainedWords") or []:
    word = item.get("word") if isinstance(item, dict) else item
    if isinstance(word, str) and word.strip():
      words.append(word.strip())
  return words


def civitai_page_link(model_id, version_id):
  """Browser link for a model version on CIVITAI_SITE_URL, or None without valid integer ids."""
  if all(isinstance(v, int) and not isinstance(v, bool) for v in (model_id, version_id)):
    return f"{CIVITAI_SITE_URL}/models/{model_id}?modelVersionId={version_id}"
  return None


def sidecar_summary(info):
  civitai = info.get("civitai")
  if isinstance(civitai, dict):
    # Rebuilt from the saved ids rather than trusting the stored link, so sidecars written
    # before the site changed also open on the current CIVITAI_SITE_URL without a re-fetch.
    civitai = {**civitai, "link": civitai_page_link(civitai.get("modelId"), civitai.get("versionId"))}
  else:
    civitai = None
  return {
    "trainedWords": trained_words_from_info(info),
    "civitai": civitai,
  }


def lora_has_trigger_words(lora_name):
  """Cheap per-lora check used for the badge on each row in the frontend."""
  full_path = folder_paths.get_full_path("loras", lora_name)
  if not full_path:
    return False
  info, err = read_sidecar(full_path)
  return False if err else bool(trained_words_from_info(info))


def sha256_of_file(full_path):
  """Streaming SHA-256 (loras can be hundreds of MB), cached by path + mtime + size."""
  st = os.stat(full_path)
  key = (full_path, st.st_mtime_ns, st.st_size)
  cached = _hash_cache.get(key)
  if cached:
    return cached
  digest = hashlib.sha256()
  with open(full_path, "rb") as f:
    for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
      digest.update(chunk)
  _hash_cache[key] = digest.hexdigest()
  return _hash_cache[key]


def fetch_civitai_by_hash(sha256):
  """Returns ("ok", payload_dict) | ("not_found", None) | ("error", message). "Not on Civitai"
  and "couldn't reach Civitai" are deliberately different outcomes."""
  req = urllib.request.Request(
    CIVITAI_BY_HASH_URL.format(sha256=sha256),
    headers={"User-Agent": "Magik-Power-Lora-Folder-Loader", "Accept": "application/json"},
  )
  try:
    with urllib.request.urlopen(req, timeout=CIVITAI_TIMEOUT_SECONDS) as resp:
      return "ok", json.loads(resp.read().decode("utf-8"))
  except urllib.error.HTTPError as e:
    if e.code == 404:
      return "not_found", None
    return "error", f"Civitai returned HTTP {e.code}."
  except urllib.error.URLError as e:
    return "error", f"Could not reach Civitai: {e.reason}"
  except OSError as e:  # includes socket timeouts
    return "error", f"Could not reach Civitai: {e}"
  except (json.JSONDecodeError, UnicodeDecodeError) as e:
    return "error", f"Unexpected response from Civitai: {e}"


def build_civitai_fields(payload, sha256):
  """Turns a Civitai model-version payload into the sidecar keys this feature owns. trainedWords
  is written as [{"word": ...}] so get_enabled_triggers_from_prompt_node reads it unchanged."""
  words = []
  for word in payload.get("trainedWords") or []:
    if isinstance(word, str) and word.strip() and {"word": word.strip()} not in words:
      words.append({"word": word.strip()})
  model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
  model_id, version_id = payload.get("modelId"), payload.get("id")
  link = civitai_page_link(model_id, version_id)
  return {
    "trainedWords": words,
    "civitai": {
      "modelId": model_id,
      "versionId": version_id,
      "name": model.get("name"),
      "versionName": payload.get("name"),
      "baseModel": payload.get("baseModel"),
      "link": link,
      "fetchedAt": int(time.time()),
    },
    "sha256": sha256,
  }


def _error(status, message):
  return status, {"status": "error", "message": message}


def get_lora_info(lora_name):
  """Blocking. Returns (http_status, body)."""
  full_path = resolve_lora_path(lora_name)
  if full_path is None:
    return _error(404, f'Unknown lora "{lora_name}".')
  info, err = read_sidecar(full_path)
  if err:
    return _error(409, err)
  return 200, {"status": "ok", **sidecar_summary(info)}


def fetch_and_store_civitai_info(lora_name):
  """Blocking (hashing + network): call through an executor. Returns (http_status, body)."""
  full_path = resolve_lora_path(lora_name)
  if full_path is None:
    return _error(404, f'Unknown lora "{lora_name}".')
  existing, err = read_sidecar(full_path)
  if err:
    return _error(409, err)
  try:
    sha256 = sha256_of_file(full_path)
  except OSError as e:
    return _error(500, f"Could not hash the lora file: {e}")
  status, payload = fetch_civitai_by_hash(sha256)
  if status == "not_found":
    return 200, {"status": "not_found", "message": "This file's hash isn't on Civitai."}
  if status == "error":
    return 200, {"status": "error", "message": payload}
  existing.update(build_civitai_fields(payload, sha256))
  try:
    write_sidecar(full_path, existing)
  except OSError as e:
    return _error(500, f"Could not write the info file: {e}")
  return 200, {"status": "ok", **sidecar_summary(existing)}


def clear_civitai_info(lora_name):
  """Blocking. Removes the keys this feature owns; deletes the sidecar only if nothing else is
  left in it. Returns (http_status, body)."""
  full_path = resolve_lora_path(lora_name)
  if full_path is None:
    return _error(404, f'Unknown lora "{lora_name}".')
  existing, err = read_sidecar(full_path)
  if err:
    return _error(409, err)
  path = sidecar_path_for(full_path)
  if os.path.isfile(path):
    for key in SIDECAR_OWNED_KEYS:
      existing.pop(key, None)
    try:
      if existing:
        write_sidecar(full_path, existing)
      else:
        os.remove(path)
    except OSError as e:
      return _error(500, f"Could not update the info file: {e}")
  return 200, {"status": "ok", "trainedWords": [], "civitai": None}


# ---------------------------------------------------------------------------
# Folder scanning
# ---------------------------------------------------------------------------

LORA_EXTENSIONS = (".safetensors", ".pt", ".ckpt", ".sft")
ROOT_FOLDER_VALUE = "."  # represents loras placed directly in a loras base dir, no subfolder


def scan_lora_subfolders():
  """Returns a sorted list of subfolder paths (POSIX-style, relative to whichever loras base
  dir they live under) found across all registered loras directories, plus the root option.
  Used to populate the "lora_folder" combo.
  """
  found = {ROOT_FOLDER_VALUE}
  for base in folder_paths.get_folder_paths("loras"):
    if not os.path.isdir(base):
      continue
    for root, dirs, _files in os.walk(base):
      dirs[:] = [d for d in dirs if not d.startswith(".")]
      rel = os.path.relpath(root, base)
      found.add(ROOT_FOLDER_VALUE if rel == "." else rel.replace(os.sep, "/"))
  return sorted(found)


def scan_loras_in_folder(folder, recursive=False):
  """Returns the lora relative paths (as known to folder_paths, POSIX-style) whose parent
  directory matches `folder`. Non-recursive by default: only direct children of that folder.
  """
  folder = (folder or ROOT_FOLDER_VALUE).strip().replace("\\", "/")
  while folder.startswith("./"):
    folder = folder[2:]
  folder = folder.strip("/") or ROOT_FOLDER_VALUE
  results = []
  for rel in folder_paths.get_filename_list("loras"):
    rel_posix = rel.replace(os.sep, "/")
    if not rel_posix.lower().endswith(LORA_EXTENSIONS):
      continue
    parent = os.path.dirname(rel_posix) or ROOT_FOLDER_VALUE
    if folder == ROOT_FOLDER_VALUE:
      match = parent == ROOT_FOLDER_VALUE
    elif recursive:
      match = parent == folder or parent.startswith(folder + "/")
    else:
      match = parent == folder
    if match:
      results.append(rel)
  return sorted(results)


def _register_folder_scan_route():
  """Registers the GET route the JS frontend polls to list loras in a chosen folder. Guarded
  so re-importing this module (e.g. on a dev hot-reload) doesn't try to register it twice.
  """
  try:
    from aiohttp import web
    from server import PromptServer
  except ImportError as e:
    log_node_warn(NODE_NAME, f"Could not import server/aiohttp to register folder-scan route: {e}")
    return

  instance = PromptServer.instance
  if instance is None or getattr(instance, "_magik_power_lora_folder_loader_route", False):
    return

  @instance.routes.get("/magik/power_lora_folder_loader/loras")
  async def _magik_power_lora_folder_loader_loras(request):  # pylint: disable=unused-variable
    folder = request.rel_url.query.get("folder", ROOT_FOLDER_VALUE)
    recursive = request.rel_url.query.get("recursive", "false").lower() == "true"
    loras = scan_loras_in_folder(folder, recursive=recursive)
    return web.json_response({
      "folder": folder,
      "loras": loras,
      # Drives the info-button badge: True when the lora's sidecar already has trigger words.
      "hasWords": {lora: lora_has_trigger_words(lora) for lora in loras},
    })

  async def run_blocking(func, *args):
    # Hashing and the Civitai request are blocking; keep them off the server's event loop.
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)

  async def lora_from_body(request):
    try:
      payload = await request.json()
    except ValueError:
      return ""
    return payload.get("lora", "") if isinstance(payload, dict) else ""

  @instance.routes.get("/magik/power_lora_folder_loader/info")
  async def _magik_power_lora_folder_loader_info(request):  # pylint: disable=unused-variable
    status, body = await run_blocking(get_lora_info, request.rel_url.query.get("lora", ""))
    return web.json_response(body, status=status)

  @instance.routes.post("/magik/power_lora_folder_loader/info/fetch")
  async def _magik_power_lora_folder_loader_info_fetch(request):  # pylint: disable=unused-variable
    status, body = await run_blocking(fetch_and_store_civitai_info, await lora_from_body(request))
    return web.json_response(body, status=status)

  @instance.routes.post("/magik/power_lora_folder_loader/info/clear")
  async def _magik_power_lora_folder_loader_info_clear(request):  # pylint: disable=unused-variable
    status, body = await run_blocking(clear_civitai_info, await lora_from_body(request))
    return web.json_response(body, status=status)

  instance._magik_power_lora_folder_loader_route = True


_register_folder_scan_route()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class MagikPowerLoraFolderLoader(io.ComfyNode):
  """Standalone Magik clone of rgthree's Power Lora Loader: applies an arbitrary number of
  loras to a model/clip, driven by dynamically-named "lora_*" optional inputs."""

  NAME = NODE_NAME

  @classmethod
  def define_schema(cls):
    folders = scan_lora_subfolders()
    return io.Schema(
      node_id="Magik.PowerLoraFolderLoader",
      display_name=NODE_NAME,
      category="Magik/Lora",
      # Any number of lora rows can be passed in from the frontend widget under keys like
      # "lora_1", "lora_2", etc.; accept_all_inputs keeps ComfyUI from rejecting them.
      accept_all_inputs=True,
      inputs=[
        # Picking a folder here is purely a UI trigger for the JS frontend, which then
        # auto-populates one "lora_x" row per lora file found in it (see the .js file).
        io.Combo.Input(
          "lora_folder",
          options=folders,
          default=folders[0] if folders else ROOT_FOLDER_VALUE,
        ),
        io.Model.Input("model", optional=True),
        io.Clip.Input("clip", optional=True),
      ],
      outputs=[
        io.Model.Output("MODEL", display_name="MODEL"),
        io.Clip.Output("CLIP", display_name="CLIP"),
      ],
    )

  @classmethod
  def validate_inputs(cls, lora_folder, model=None, clip=None, **kwargs):
    return True

  @classmethod
  def execute(cls, model=None, clip=None, lora_folder=None, **kwargs):
    """Loops over the provided lora_* kwargs and applies the enabled ones in order.

    `lora_folder` itself isn't used here -- it only drives which "lora_x" rows the JS
    frontend auto-populates. The rows already carry their own resolved lora path, so
    execution doesn't need to re-scan the folder.
    """
    del lora_folder
    for key, value in kwargs.items():
      key = key.upper()
      if key.startswith("LORA_") and "on" in value and "lora" in value and "strength" in value:
        strength_model = value["strength"]
        # A single strength value drives both model and clip unless a distinct strengthTwo
        # was supplied, in which case strength is model-only and strengthTwo is clip-only.
        strength_clip = value["strengthTwo"] if "strengthTwo" in value else None
        if clip is None:
          if strength_clip is not None and strength_clip != 0:
            log_node_warn(NODE_NAME, "Received clip strength even though no clip was supplied!")
          strength_clip = 0
        else:
          strength_clip = strength_clip if strength_clip is not None else strength_model
        if value["on"] and (strength_model != 0 or strength_clip != 0):
          lora = get_lora_by_filename(value["lora"], log_node=cls.NAME)
          if model is not None and lora is not None:
            model, clip = LoraLoader().load_lora(model, clip, lora, strength_model, strength_clip)

    return io.NodeOutput(model, clip)

  @classmethod
  def get_enabled_loras_from_prompt_node(cls, prompt_node: dict) -> list:
    """Gets enabled loras of a node within a server prompt."""
    result = []
    for name, lora in prompt_node["inputs"].items():
      if name.startswith("lora_") and lora["on"]:
        lora_file = get_lora_by_filename(lora["lora"], log_node=cls.NAME)
        if lora_file is not None:
          lora_dict = {
            "name": lora["lora"],
            "strength": lora["strength"],
            "path": folder_paths.get_full_path("loras", lora_file),
          }
          if "strengthTwo" in lora:
            lora_dict["strength_clip"] = lora["strengthTwo"]
          result.append(lora_dict)
    return result

  @classmethod
  def get_enabled_triggers_from_prompt_node(cls, prompt_node: dict, max_each: int = 1):
    """Gets trigger words up to the max for enabled loras of a node within a server prompt."""
    loras = [l["name"] for l in cls.get_enabled_loras_from_prompt_node(prompt_node)]
    trained_words = []
    for lora in loras:
      info = get_lora_info_file_data(lora, default={})
      if not info or not info.keys():
        log_node_warn(
          NODE_NAME,
          f'No info found for lora "{lora}" when grabbing triggers. Add a matching '
          '"<lora>.json" sidecar with a "trainedWords" list to enable this.'
        )
        continue
      if "trainedWords" not in info or not info["trainedWords"]:
        log_node_warn(NODE_NAME, f'No trained words listed for lora "{lora}".')
        continue
      trained_words += [w for wi in info["trainedWords"][:max_each] if (wi and (w := wi["word"]))]
    return trained_words
