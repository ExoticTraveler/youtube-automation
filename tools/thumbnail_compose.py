"""Editorial background composition for the thumbnail production pipeline.

Four-concept architecture: for each of A/B/C/D pick 1-3 real source frames and
ask the image-edit model to recompose them into ONE final background-only 16:9
candidate (exactly one paid image edit per concept, no baseline/documentary or
intensity variants), generating only connective pixels.
Nothing here silently degrades: an image-API failure raises.
"""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SELECT_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"concepts": {
    "type": "array", "minItems": 4, "maxItems": 4, "items": {"type": "object", "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "enum": ["A", "B", "C", "D"]},
        "ingredients": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "object", "additionalProperties": False,
            "properties": {"index": {"type": "integer"}, "ingredient": {"type": "string"},
                           "role": {"type": "string", "enum": ["base", "context", "attention_object"]}},
            "required": ["index", "ingredient", "role"]}},
        "composition_brief": {"type": "string"},
        "attention_object": {"type": "string"}},
    "required": ["id", "ingredients", "composition_brief", "attention_object"]}}}, "required": ["concepts"]}

SELECT_PROMPT = """You are choosing REAL source frames (numbered contact sheets) that an image model will recompose into FOUR final thumbnail backgrounds, one each for concepts A, B, C and D, for a travel-documentary episode.
Every background is produced by one paid image edit and must be a serious FINAL thumbnail candidate. There are no baseline, documentary-reference, lower-intensity or stepping-stone versions.
For each concept pick 1-3 frames and say precisely what real ingredient each contributes (role base/context/attention_object). Use frame numbers exactly as labelled. Declare ONE concrete physical attention_object per concept that is actually visible in a chosen frame and is the single focal subject of that thumbnail.
Frame rules: prefer frames with a strong legible place, visual depth and little or no prominent people; NEVER pick frames dominated by the channel creator or close-up faces; avoid motion-blurred, dark or lens-obstructed frames unless essential evidence. Prefer the creator's OWN field footage everywhere. Third-party material inside the episode (news/TV clips, archival footage, screen recordings, anything with a broadcaster caption, logo, ticker or baked-in graphic) must NOT be the dominant source of any concept; use it only if the episode has no usable field footage for that concept and there is an unusually strong editorial reason.
Concepts (all are attention-oriented finals):
  A = JOURNEY / PLACE: the physical journey itself (border crossings, roads, transport, movement, distinctive geography or a recognizable place from the episode: roads, streetscapes, landscapes, mountains, city approaches, skylines, architecture, transport corridors). The viewer should immediately feel "Where is this person going?" / "What is this place?". Never a ceremony, protest, interview, crowd event, political symbolism without place context, close-up of people or generic object close-up as A's subject unless the footage genuinely has no usable journey/place material.
  B = GEOPOLITICAL / TENSION: political, social or geopolitical context that genuinely exists in the episode, shown through the creator's own field footage (border infrastructure, official symbolism, posters, murals, flags, monuments, signage, a real checkpoint or military/political presence only if it was actually filmed). Never fabricate danger, military activity, explosions, conflict or events; never make third-party news footage the dominant source merely because it looks dramatic.
  C = EVERYDAY CONTRAST / REALITY: a visually strong contrast between outside expectations of the country and ordinary reality on the ground (streets, markets, money, people, commerce, food, architecture, everyday life). This must NOT be a boring everyday documentary frame: choose an object or composition with immediate visual interest (e.g. a vivid market scene, a striking real object such as banknotes, distinctive food or architecture).
  D = HIGH-ATTENTION EDITORIAL: the most aggressive candidate, optimized to stop attention in a small YouTube feed: one immediately understandable focal subject, strong visual hierarchy, clear depth/composition, minimal clutter, strong curiosity/tension. Compose it noticeably more aggressively than A, B and C (scale, perspective, hierarchy, symbolic episode evidence) without any fabrication.
Every attention_object must have thumbnail value: ask "would this still create immediate visual curiosity when the thumbnail is viewed very small?" Prefer visually distinctive, instantly legible, episode-specific, contextually meaningful or curiosity-producing things (unusual architecture, monument, mural, national/political symbolism, distinctive vehicle, geographic feature, unusual infrastructure, a striking real object). A mundane device, dashboard, car interior, ordinary phone or other small object must NOT be chosen just because it is available, unless it is genuinely central to the episode story and visually meaningful.
Use different source frames and different attention objects across the four concepts where the episode allows it; do not repeat the same composition.
composition_brief: 2-3 sentences of spatial layout instructions (what is foreground/midground/background and where the main subject sits). Keep the upper-left and lower-right corners visually quiet (title and flag go there later).
Never invent violence, soldiers, checkpoints, protests, destruction, fire, explosions or other events. Return JSON matching the schema."""

COMMON = ("Create ONE photorealistic 16:9 documentary thumbnail BACKGROUND ONLY for a travel episode about {country}. "
          "Use exactly the attached real frames as sources: {sources}. "
          "Generate only the connective pixels needed for a coherent result (sky, terrain, road surfaces, vegetation, atmosphere, occlusion edges, perspective/light/colour/grain/sharpness matching). "
          "Keep the identity and design of each ingredient faithful to its frame (architecture, vehicles, signage, banners, flags, geography). "
          "Protected zones: keep the upper-left corner (about 31% width x 21% height) and the extreme lower-right corner (about 39% width x 39% height) visually quiet, low-detail and free of key subjects, because a title and flag are added later. "
          "Leave the left side relatively open and uncluttered so a person can be added later. "
          "Forbidden: any person in close-up, the creator, text or titles, captions or broadcaster graphics copied from the sources, flag overlays, arrows, circles, logos, emoji, watermarks, and any invented violence, soldiers, guards, checkpoints, protests, crowds, destruction, fire, explosions, smoke, dramatic weather or invented event. Ordinary distant street life is allowed only if it is in the sources.")
ATTENTION = ("Concept: {proposition}. FINAL THUMBNAIL background: make the {obj} the single dominant focal subject through scale, strong perspective (e.g. low camera angle), a clear foreground-midground-background depth system and an uncluttered visual hierarchy, with the other ingredients as supporting context. Vivid, well-exposed, high-clarity light with strong tonal separation that reads at small thumbnail size (a night source may remain night but must be clearly exposed). Bold but photographic and credible, with no invented event. Do not reproduce any captions, tickers, broadcaster logos or on-screen graphics that appear in the source frames. Layout: {brief}")
HIGH_ATTENTION = ("Concept: {proposition}. MAXIMUM-ATTENTION FINAL THUMBNAIL background, the most aggressive candidate of the set: ONE immediately understandable focal subject ({obj}) at dramatic scale, with an extreme but credible perspective (low or close camera, strong leading lines), strong subject/background separation and visual hierarchy, clear depth, minimal clutter and intense curiosity/tension created only by composition and light. Push scale, contrast and clarity much harder than an ordinary frame, yet stay photographic and true to what the source frames actually show: absolutely no invented explosions, soldiers, danger, events or situations. Do not reproduce any captions, tickers, broadcaster logos or on-screen graphics that appear in the source frames. Layout: {brief}")

def _post_json(payload, key, timeout):
    req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI ingredient selection HTTP {e.code}; body omitted") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("OpenAI ingredient selection network error/timeout") from None


def select_ingredients(sheets, names, plan, key, model):
    """One vision call: choose real frames for the four final concepts. Returns {id: selection}, usage."""
    def b64(p): return "data:image/jpeg;base64," + base64.b64encode(Path(p).read_bytes()).decode()
    brief = [{"id": c["id"], "proposition": c["proposition"], "evidence": c["evidence"],
              "attention_object_hint": c.get("attention_object"), "timestamp_ranges": c["timestamp_ranges"]} for c in plan["concepts"]]
    content = [{"type": "input_text", "text": json.dumps({"episode_summary": plan["episode_summary"], "concepts": brief,
                "frame_timestamps_seconds": {str(i): n["timestamp_seconds"] for i, n in enumerate(names)}}, ensure_ascii=False)}]
    content += [{"type": "input_image", "image_url": b64(s)} for s in sheets]
    payload = {"model": model, "store": False, "instructions": SELECT_PROMPT, "input": [{"role": "user", "content": content}],
               "max_output_tokens": 9000, "text": {"format": {"type": "json_schema", "name": "ingredient_selection", "strict": True, "schema": SELECT_SCHEMA}}}
    result = _post_json(payload, key, 180)
    parts = [p for i in result.get("output", []) if i.get("type") == "message" for p in i.get("content", [])]
    if result.get("status") != "completed" or any(p.get("type") == "refusal" for p in parts):
        raise RuntimeError("OpenAI ingredient selection incomplete/refused")
    sel = json.loads("".join(p.get("text", "") for p in parts if p.get("type") == "output_text"))["concepts"]
    out = {}
    for c in sel:
        if "variants" in c:
            raise RuntimeError(f"Concept {c['id']}: legacy multi-variant selection is not allowed (one final candidate per concept)")
        for ing in c["ingredients"]:
            if not 0 <= ing["index"] < len(names):
                raise RuntimeError(f"Ingredient selection returned unknown frame index {ing['index']}")
        if not (c.get("attention_object") or "").strip():
            raise RuntimeError(f"{c['id']}: attention_object not declared")
        if c["id"] in out:
            raise RuntimeError(f"Concept {c['id']} selected twice")
        out[c["id"]] = c
    if sorted(out) != ["A", "B", "C", "D"]:
        raise RuntimeError("Ingredient selection must cover A/B/C/D")
    return out, result.get("usage")


def extract_source(ffmpeg, video, seconds, target):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "2", "-vf", "scale='min(1920,iw)':-2:flags=lanczos", "-y", str(target)], check=True)


def concept_kind(cid):
    return "high_attention" if cid == "D" else "attention"


def build_prompt(concept, plan, selection, sources, kind):
    src = "; ".join(f"image {i+1} = real frame at {s['timestamp']} showing {s['ingredient']} (role: {s['role']})" for i, s in enumerate(sources))
    common = COMMON.format(country=plan["country_name"], sources=src)
    template = HIGH_ATTENTION if kind == "high_attention" else ATTENTION
    return common + " " + template.format(proposition=concept["proposition"], obj=selection["attention_object"], brief=selection["composition_brief"])


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        buf.write(path.read_bytes()); buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def edit_image(prompt, source_paths, key, model, quality, size="1536x864", attempts=3):
    body, ctype = _multipart({"model": model, "prompt": prompt, "size": size, "quality": quality, "n": "1"},
                             [("image[]", p) for p in source_paths])
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request("https://api.openai.com/v1/images/edits", data=body,
                                     headers={"Authorization": "Bearer " + key, "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=420) as r:
                result = json.load(r)
            return base64.b64decode(result["data"][0]["b64_json"]), result.get("usage")
        except urllib.error.HTTPError as e:
            detail = ""
            try: detail = json.load(e).get("error", {}).get("code") or ""
            except Exception: pass
            last = f"HTTP {e.code} {detail}".strip()
            if e.code not in (429, 500, 502, 503, 504): break
        except (urllib.error.URLError, TimeoutError):
            last = "network error/timeout"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"OpenAI image edit failed ({last}); no degraded output produced")


def compose_backgrounds(plan, chosen, frames_info, video, ffmpeg, tmp, deps, key, model, quality, canvas):
    """chosen: concept id -> selection. Exactly ONE paid image edit per concept (A/B/C/D), each a final candidate.
    Returns one record per concept; raw images saved to tmp/backgrounds_raw/<ID>.jpg."""
    Image = deps[0]
    (tmp / "sources").mkdir(exist_ok=True); (tmp / "backgrounds_raw").mkdir(exist_ok=True)
    jobs = []
    for concept in plan["concepts"]:
        cid = concept["id"]; sel = chosen[cid]; sources = []; paths = []
        for k, ing in enumerate(sel["ingredients"], 1):
            fr = frames_info[ing["index"]]
            p = tmp / "sources" / f"{cid}_{k}_{fr['timestamp'].replace(':', '-')}.jpg"
            if not p.exists(): extract_source(ffmpeg, video, fr["timestamp_seconds"], p)
            sources.append({"timestamp": fr["timestamp"], "timestamp_seconds": fr["timestamp_seconds"], "file": str(p.relative_to(tmp)),
                            "ingredient": ing["ingredient"], "role": ing["role"]}); paths.append(p)
        kind = concept_kind(cid)
        jobs.append((concept, kind, sel, sources, paths, build_prompt(concept, plan, sel, sources, kind)))

    def run(job):
        concept, kind, sel, sources, paths, prompt = job
        cid = concept["id"]; raw = tmp / "backgrounds_raw" / f"{cid}.jpg"
        data, usage = edit_image(prompt, paths, key, model, quality)   # the ONLY paid image call for this concept
        im = Image.open(io.BytesIO(data)).convert("RGB").resize(canvas, Image.Resampling.LANCZOS)
        import numpy as np
        if float(np.asarray(im.convert("L")).std()) < 8: raise RuntimeError(f"{cid}: generated image is blank/flat")
        im.save(raw, quality=95, subsampling=0)
        return {"id": cid, "concept": cid, "variant": 1, "kind": kind, "raw": raw, "sources": sources,
                "prompt": prompt, "usage": usage, "attention_object": sel.get("attention_object"),
                "composition_brief": sel["composition_brief"], "strategy": concept["proposition"], "episode_evidence": concept["evidence"]}
    with ThreadPoolExecutor(max_workers=4) as ex:
        return list(ex.map(run, jobs))
