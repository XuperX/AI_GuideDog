"""Luma: spoken navigation previews for blind and low-vision travelers.

Searches indexed street, path, and indoor cameras and answers with a short
brief: Stop, Wait, or Clear. The footage is recorded. Briefs describe what
the cameras saw and never tell someone to step into the road.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
VSS_URL = os.environ.get("VSS_URL") or os.environ.get("INGRESS_URL") or ""
VSS_USERNAME = os.environ.get("VSS_USERNAME") or os.environ.get("USERNAME") or ""
VSS_PASSWORD = os.environ.get("VSS_PASSWORD") or os.environ.get("PASSWORD") or ""
HERE = Path(__file__).resolve().parent
INDEX_HTML = (HERE / "index.html").read_text(encoding="utf-8")

SYSTEM_PROMPT = (
    "You write a spoken navigation brief for a blind traveler. "
    "This is recorded camera video, not their live surroundings. "
    "Never tell them to cross, step off a curb, or enter a road or aisle. "
    "Use plain words: person, vehicle, crossing, curb, ahead, left, right, blocked. "
    "No markdown. Two short sentences: what is in the path, then what they should "
    "verify with a cane, a sounding signal, or a person with them."
)

RELEVANT_OBJECTS = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "bus",
    "truck",
    "traffic light",
    "stop sign",
    "bench",
    "backpack",
    "handbag",
    "suitcase",
    "dog",
)

STOP_PHRASES = (
    "crossing the street",
    "crosses the street",
    "crossing from",
    "in the crosswalk",
    "in the roadway",
    "middle of the road",
    "walks across",
    "walking across",
    "pedestrian is crossing",
    "pedestrian crosses",
    "person is crossing",
    "forklift",
    "near miss",
    "braking hard",
    "runs into",
    "steps into the road",
)

VEHICLE_KEYS = ("car", "truck", "bus", "motorcycle")

CAMERA_LABELS = {
    "pie_cam-3": "Toronto dashcam",
    "neighborhood_cam-1": "Neighborhood camera",
    "nyc_bike_gopro-1": "New York bike camera",
    "wearable-1": "Eye-level sidewalk camera",
    "sidewalk_pov": "Sidewalk walking clips",
    "nyc_streets_cam-1": "New York street camera 1",
    "nyc_streets_cam-2": "New York street camera 2",
    "sf_streets_cam-1": "San Francisco street camera 1",
    "sf_streets_cam-2": "San Francisco street camera 2",
    "sf_streets_cam-3": "San Francisco street camera 3",
    "sf_streets_cam-4": "San Francisco street camera 4",
    "sf_streets_cam-5": "San Francisco street camera 5",
    "smartspace_cam-1": "Indoor camera",
    "sdg_warehouse_cam-2": "Warehouse camera",
    "i24_cam-1": "Highway camera",
}

PLACES = {
    "toronto": {
        "label": "Toronto intersections",
        "detail": "Dashcam views of crossings, curbs, and turning traffic.",
        "location": "toronto",
        "prompts": [
            "Is a person crossing while cars are moving?",
            "Is a vehicle stopped near people at the curb?",
            "Are cones or barriers blocking the crossing?",
        ],
    },
    "sf": {
        "label": "San Francisco streets",
        "detail": "Street cameras at San Francisco blocks and intersections.",
        "location": "san_francisco",
        "prompts": [
            "Are people crossing while cars are nearby?",
            "Is the intersection busy with vehicles?",
            "Is anyone waiting at the curb?",
        ],
    },
    "nyc": {
        "label": "New York streets",
        "detail": "Street cameras in New York. Bike-camera clips are left out of this place.",
        "location": "new_york",
        "street_only": True,
        "prompts": [
            "Is a person near moving cars?",
            "What is happening at the intersection?",
            "Are vehicles stopped near people on foot?",
        ],
    },
    "neighborhood": {
        "label": "Neighborhood block",
        "detail": "A residential street camera facing the curb and passing cars.",
        "location": "neighborhood",
        "prompts": [
            "Is a car stopping at the curb?",
            "Are two vehicles close together near the sidewalk?",
            "Is the street in front of the houses busy?",
        ],
    },
    "sidewalk": {
        "label": "Sidewalk, eye level",
        "detail": "Walking views of sidewalks, curbs, and crosswalks, including a first-person sidewalk camera.",
        "location": "sidewalk",
        "prompts": [
            "Is someone walking in the crosswalk while cars are nearby?",
            "Is the sidewalk ahead blocked by a person or an object?",
            "Is there a curb between the sidewalk and moving vehicles?",
        ],
    },
    "bike": {
        "label": "New York bike camera",
        "detail": "A forward-facing bike camera. It shows the path ahead of the rider.",
        "location": "new_york",
        "camera_id": "nyc_bike_gopro-1",
        "prompts": [
            "What people or vehicles are ahead?",
            "Is anything blocking the way forward?",
            "Is a pedestrian close to the bike?",
        ],
    },
    "indoor": {
        "label": "Indoor corridors",
        "detail": "Cameras inside a building, along corridors and open floor areas.",
        "location": "indoor",
        "prompts": [
            "Is the corridor clear to walk through?",
            "Are people blocking the walkway?",
            "Is the hallway empty?",
        ],
    },
    "warehouse": {
        "label": "Warehouse aisles",
        "detail": "Aisle cameras where people and forklifts share the floor.",
        "location": "warehouse3",
        "prompts": [
            "Is a forklift near a person in the aisle?",
            "Is the walkway blocked by a pallet or vehicle?",
            "Is a person in the aisle?",
        ],
    },
}

MODES = [
    {
        "id": "cross",
        "label": "Cross the street",
        "description": "Crossings, curbs, and cars near people on foot.",
        "places": ["sidewalk", "toronto", "sf", "nyc", "neighborhood"],
    },
    {
        "id": "ahead",
        "label": "Check the path ahead",
        "description": "Eye-level walking views and a forward-facing bike camera.",
        "places": ["sidewalk", "bike"],
    },
    {
        "id": "indoor",
        "label": "Move through a building",
        "description": "Indoor corridors and warehouse aisles.",
        "places": ["indoor", "warehouse"],
    },
]

ADVICE = {
    "cross": {
        "stop": (
            "Do not treat this as a signal to move. Check with your cane, a crossing "
            "signal you can hear, or a person with you."
        ),
        "wait": (
            "Pause before you move. Confirm the crossing with your cane, a signal you "
            "can hear, or a person with you."
        ),
        "clear": (
            "The closest recorded moment does not show a person in the road with a "
            "vehicle. Still check the curb yourself before you move."
        ),
    },
    "ahead": {
        "stop": (
            "Do not treat this as a signal to move forward. Stop and check the path "
            "with your cane or a person with you."
        ),
        "wait": (
            "Pause before you move forward. People or vehicles are in this recorded "
            "view. Check the path yourself."
        ),
        "clear": (
            "The closest recorded moment does not show a person or vehicle blocking "
            "the way ahead. Still check the path yourself before you move."
        ),
    },
    "indoor": {
        "stop": (
            "Do not treat this as a signal to walk on. A person and a vehicle or "
            "forklift share this aisle. Check it with your cane or a person with you."
        ),
        "wait": (
            "Pause before you walk on. Confirm the aisle is empty with your cane or "
            "a person with you."
        ),
        "clear": (
            "The closest recorded moment shows an open path. Still check the floor "
            "ahead with your cane before you walk on."
        ),
    },
}
ADVICE_NONE = "Try a simpler question, such as whether a person is near a vehicle."
OBJECT_WORDS = {
    "person": ("person", "people"),
    "bus": ("bus", "buses"),
    "traffic light": ("traffic light", "traffic lights"),
    "stop sign": ("stop sign", "stop signs"),
}

RECORDED = "This is recorded video, not where you are standing right now."

# Documented hackathon GPU host. Non-secret URLs can be overridden from the environment.
GPU_HOST = os.environ.get("GPU_HOST") or "166.19.38.112"
COSMOS3_REASON_URL = (os.environ.get("COSMOS3_REASON_URL") or f"http://{GPU_HOST}:8001").rstrip("/")
YOLO_URL = (os.environ.get("YOLO_URL") or f"http://{GPU_HOST}:8002").rstrip("/")
COSMOS3_REASON_MODEL = os.environ.get("COSMOS3_REASON_MODEL") or "nvidia/cosmos3-nano-reasoner"
GPU_BEARER_TOKEN = os.environ.get("GPU_BEARER_TOKEN") or ""

LOOK_PROMPT = (
    "You are looking at one frame from a camera held by a person who cannot see. "
    "Describe what is in the frame in one or two short sentences. "
    "Say what is ahead, what it is doing, and whether it is in the way. "
    "Describe only what is visible. Do not give directions. "
    "Do not say Stop, Wait, or Clear. "
    "Do not say to cross, step off a curb, or enter a road. "
    "Do not mention a cane, a sounding signal, or someone accompanying the traveler."
)
JUDGE_PROMPT = (
    "You are judging a description of one camera frame for a person who cannot see. "
    "Read the description and choose one signal. "
    "Reply with exactly two lines and nothing else:\n"
    "SIGNAL: Clear or Wait or Stop\n"
    "WHY: one short reason from the description.\n"
    "Clear means the road or path ahead is open, or nothing is in the way. "
    "Wait means a hazard is approaching but is not yet in the way. "
    "Stop means something is an immediate blocker, already in the way. "
    "Do not give directions. Do not say to cross, step off a curb, or enter a road.\n"
    "Description:\n"
)
LOOK_LABELS = {"stop": "Stop", "wait": "Wait", "clear": "Clear"}


class VssClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._lock = threading.Lock()

    def _login(self) -> str:
        if not VSS_URL or not VSS_USERNAME or not VSS_PASSWORD:
            raise RuntimeError("Video search is not configured on this server.")
        payload = json.dumps(
            {"username": VSS_USERNAME, "password": VSS_PASSWORD}
        ).encode()
        request = urllib.request.Request(
            VSS_URL.rstrip("/") + "/api/v1/auth/login",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            token = json.load(response)["access_token"]
        self._token = token
        return token

    def token(self) -> str:
        with self._lock:
            if self._token:
                return self._token
            return self._login()

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def post_json(self, path: str, body: dict) -> dict:
        last_error: Exception | None = None
        for attempt in (1, 2):
            token = self.token()
            request = urllib.request.Request(
                VSS_URL.rstrip("/") + path,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code == 401 and attempt == 1:
                    self.invalidate()
                    continue
                detail = error.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"Video search failed ({error.code}). {detail}") from error
            except urllib.error.URLError as error:
                raise RuntimeError("Video search could not be reached.") from error
        raise RuntimeError("Video search rejected the login.") from last_error

    def get_json(self, path: str) -> dict:
        last_error: Exception | None = None
        for attempt in (1, 2):
            token = self.token()
            request = urllib.request.Request(
                VSS_URL.rstrip("/") + path,
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code == 401 and attempt == 1:
                    self.invalidate()
                    continue
                detail = error.read().decode("utf-8", "replace")[:300]
                raise RuntimeError(f"Video search failed ({error.code}). {detail}") from error
            except urllib.error.URLError as error:
                raise RuntimeError("Video search could not be reached.") from error
        raise RuntimeError("Video search rejected the login.") from last_error

    def open_stream(self, source: str, range_header: str | None):
        last_error: Exception | None = None
        for attempt in (1, 2):
            token = self.token()
            query = urllib.parse.urlencode({"source": source, "token": token})
            url = VSS_URL.rstrip("/") + "/api/v1/videos/stream?" + query
            headers = {}
            if range_header:
                headers["Range"] = range_header
            request = urllib.request.Request(url, headers=headers)
            try:
                return urllib.request.urlopen(request, timeout=60)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code == 401 and attempt == 1:
                    self.invalidate()
                    continue
                raise
        raise last_error  # type: ignore[misc]


CLIENT = VssClient()


def catalog() -> dict:
    modes = []
    for mode in MODES:
        modes.append(
            {
                "id": mode["id"],
                "label": mode["label"],
                "description": mode["description"],
                "places": [
                    {
                        "id": place_id,
                        "label": PLACES[place_id]["label"],
                        "detail": PLACES[place_id]["detail"],
                        "prompts": PLACES[place_id]["prompts"],
                    }
                    for place_id in mode["places"]
                ],
            }
        )
    return {"modes": modes}


def parse_counts(raw) -> dict[str, int]:
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    else:
        return {}
    counts: dict[str, int] = {}
    for key, value in data.items():
        try:
            counts[str(key).lower()] = int(value)
        except (TypeError, ValueError):
            continue
    return counts


def first_sentence(text: str) -> str:
    cleaned = " ".join((text or "").split())
    cleaned = cleaned.replace("ego vehicle", "camera vehicle")
    cleaned = cleaned.replace("Ego vehicle", "Camera vehicle")
    if not cleaned:
        return ""
    for index, char in enumerate(cleaned):
        if char in ".!?" and index > 40:
            return cleaned[: index + 1]
    return cleaned[:280].rstrip(" ,;") + ("." if not cleaned[:280].endswith(".") else "")


def relevant_objects(counts: dict[str, int]) -> list[str]:
    found = []
    for name in RELEVANT_OBJECTS:
        amount = counts.get(name, 0)
        if amount <= 0:
            continue
        singular, plural = OBJECT_WORDS.get(name, (name, name + "s"))
        found.append(f"{amount} {singular if amount == 1 else plural}")
    return found


def merged_counts(results: list[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for result in results[:3]:
        for key, value in parse_counts(result.get("object_counts")).items():
            totals[key] = totals.get(key, 0) + value
    return totals


def decide(results: list[dict]) -> str:
    if not results:
        return "none"
    texts = [(result.get("reasoning_content") or "").lower() for result in results[:3]]
    text = "\n".join(texts)
    counts = merged_counts(results)
    people = counts.get("person", 0)
    vehicles = sum(counts.get(key, 0) for key in VEHICLE_KEYS)
    for snippet in texts:
        vehicle_words = vehicles or any(
            word in snippet for word in ("vehicle", "car", "suv", "truck", "bus")
        )
        if "forklift" in snippet and (people or "person" in snippet):
            return "stop"
        if any(phrase in snippet for phrase in STOP_PHRASES) and vehicle_words:
            return "stop"
    if people and vehicles:
        return "wait"
    if vehicles and any(word in text for word in ("intersection", "crossing", "curb", "crosswalk")):
        return "wait"
    if any(word in text for word in ("blocked", "obstacle", "cones")):
        return "wait"
    if people == 0 and vehicles == 0 and any(
        word in text for word in ("empty", "clear", "no pedestrian", "no vehicle", "no people")
    ):
        return "clear"
    return "wait"


def camera_label(camera_id: str, location: str) -> str:
    if camera_id in CAMERA_LABELS:
        return CAMERA_LABELS[camera_id]
    place = (location or "this place").replace("_", " ")
    return f"{place} camera"


def clock(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "an unknown time"
    minutes, secs = divmod(max(total, 0), 60)
    if minutes:
        return f"{minutes} minute{'s' if minutes != 1 else ''} and {secs} seconds"
    return f"{secs} seconds"


def clock_short(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?:??"
    minutes, secs = divmod(max(total, 0), 60)
    return f"{minutes}:{secs:02d}"


def filters_for(place: dict) -> dict:
    filters = {"location": place["location"]}
    if place.get("camera_id"):
        filters["camera_id"] = place["camera_id"]
    return filters


def keep_result(place: dict, result: dict) -> bool:
    if not place.get("street_only"):
        return True
    camera = (result.get("camera_id") or "").lower()
    return "bike" not in camera and "gopro" not in camera


def moment_from(result: dict) -> dict:
    counts = parse_counts(result.get("object_counts"))
    objects = relevant_objects(counts)
    start = result.get("segment_start_sec")
    end = result.get("segment_end_sec")
    if start is None:
        start = result.get("best_match_start_sec")
    if end is None:
        end = result.get("best_match_end_sec")
    source = result.get("source") or result.get("preview_source") or ""
    camera_id = result.get("camera_id") or ""
    location = result.get("location") or ""
    return {
        "camera": camera_label(camera_id, location),
        "camera_id": camera_id,
        "location": location,
        "start_label": clock(start),
        "end_label": clock(end),
        "start_short": clock_short(start),
        "end_short": clock_short(end),
        "sentence": first_sentence(result.get("reasoning_content") or ""),
        "objects": objects,
        "source": source,
        "score": result.get("similarity_score"),
    }


def matching_sentence(text: str, phrases: tuple[str, ...]) -> str:
    cleaned = " ".join((text or "").split())
    cleaned = cleaned.replace("ego vehicle", "camera vehicle")
    cleaned = cleaned.replace("Ego vehicle", "Camera vehicle")
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    for part in parts:
        lowered = part.lower()
        if any(phrase in lowered for phrase in phrases):
            return part.strip()[:320]
    return first_sentence(text)


def pick_scene(results: list[dict], status: str) -> str:
    if not results:
        return ""
    if status == "stop":
        phrases = STOP_PHRASES + ("forklift",)
        for result in results[:3]:
            text = result.get("reasoning_content") or ""
            lowered = text.lower()
            if any(phrase in lowered for phrase in phrases):
                return matching_sentence(text, phrases)
    return first_sentence(results[0].get("reasoning_content") or "")


def spoken_brief(mode_id: str, status: str, scene: str, objects: list[str]) -> str:
    label = {"stop": "Stop.", "wait": "Wait.", "clear": "Clear.", "none": "No match."}[status]
    parts = [label]
    if objects:
        parts.append("In view: " + ", ".join(objects) + ".")
    if scene:
        parts.append(scene)
    if status == "none":
        parts.append(ADVICE_NONE)
    else:
        parts.append(ADVICE[mode_id][status])
    parts.append(RECORDED)
    return " ".join(parts)


def walk_cue(text: str) -> str:
    cleaned = " ".join((text or "").split())
    cleaned = cleaned.replace("ego vehicle", "camera vehicle")
    cleaned = cleaned.replace("Ego vehicle", "Camera vehicle")
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    wanted = (
        "ahead",
        "left",
        "right",
        "sidewalk",
        "crosswalk",
        "curb",
        "walking",
        "walks",
        "vehicle",
        "car",
        "bus",
        "blocked",
        "person",
        "people",
    )
    chosen = [part for part in parts if any(word in part.lower() for word in wanted)]
    if not chosen:
        chosen = parts[:1] or ["The path continues."]
    cue = " ".join(chosen[:2])
    return cue[:420]


def walk_route(place_id: str) -> dict:
    place = PLACES.get(place_id)
    if place is None:
        raise ValueError("Choose a place first.")
    data = CLIENT.post_json(
        "/api/v1/search",
        {
            "query": "person walking along a sidewalk, curb, or crosswalk",
            "top_k": 20,
            "llm_top_n": 1,
            "min_similarity": 0.12,
            "time_filter": "all",
            "metadata_filters": filters_for(place),
            "include_public": True,
        },
    )
    grouped: dict[str, list[dict]] = {}
    for row in data.get("results") or []:
        if not keep_result(place, row):
            continue
        parent = row.get("original_video") or ""
        if parent:
            grouped.setdefault(parent, []).append(row)
    if not grouped:
        raise ValueError("No walk is indexed for this place yet.")
    parent = max(grouped, key=lambda key: (len(grouped[key]), grouped[key][0].get("total_segments") or 0))
    listed = CLIENT.get_json(
        "/api/v1/tools/segments?original_video=" + urllib.parse.quote(parent, safe="")
    )
    segments = listed.get("segments") or grouped[parent]
    segments = sorted(segments, key=lambda row: row.get("segment_number") or 0)
    steps = []
    total = len(segments)
    for index, row in enumerate(segments, start=1):
        moment = moment_from(row)
        cue = walk_cue(row.get("reasoning_content") or "")
        steps.append(
            {
                **moment,
                "index": index,
                "total": total,
                "spoken": (
                    f"Step {index} of {total}. {cue} "
                    "This is recorded video, not where you are standing right now."
                ),
            }
        )
    if not steps:
        raise ValueError("That walk has no steps yet.")
    return {
        "place_label": place["label"],
        "title": place["label"],
        "steps": steps,
    }


def strip_markdown(text: str) -> str:
    cleaned = re.sub(r"[#*_`>]", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:420]


def guide(mode_id: str, place_id: str, question: str) -> dict:
    question = " ".join((question or "").split())
    if not question:
        raise ValueError("Ask a question first.")
    mode = next((item for item in MODES if item["id"] == mode_id), None)
    if mode is None or place_id not in mode["places"]:
        raise ValueError("Choose a place first.")
    place = PLACES[place_id]
    payload = {
        "query": question,
        "top_k": 8,
        "llm_top_n": 3,
        "min_similarity": 0.18,
        "time_filter": "all",
        "metadata_filters": filters_for(place),
        "include_public": True,
        "system_prompt": SYSTEM_PROMPT,
    }
    data = CLIENT.post_json("/api/v1/search", payload)
    results = [row for row in (data.get("results") or []) if keep_result(place, row)]
    if not results and place.get("street_only"):
        results = list(data.get("results") or [])
    results = results[:5]
    status = decide(results)
    moments = [moment_from(row) for row in results[:3]]
    scene = pick_scene(results, status)
    objects = moments[0]["objects"] if moments else []
    synthesis = data.get("llm_synthesis") or {}
    detail = strip_markdown(synthesis.get("response") or "")
    lowered = detail.lower()
    if any(
        phrase in lowered
        for phrase in (
            "step off",
            "cross now",
            "you should cross",
            "go ahead and cross",
            "enter the road",
            "walk into the",
        )
    ):
        detail = ""
    return {
        "status": status,
        "status_label": {"stop": "Stop", "wait": "Wait", "clear": "Clear", "none": "No match"}[
            status
        ],
        "spoken": spoken_brief(mode_id, status, scene, objects),
        "detail": detail,
        "place_label": place["label"],
        "question": question,
        "moments": moments,
    }


def _gpu_json(url: str, body: dict, timeout: int) -> dict:
    if not GPU_BEARER_TOKEN:
        raise RuntimeError("look model is not configured")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GPU_BEARER_TOKEN}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        error.read()
        print(f"[luma] look model http {error.code}")
        raise RuntimeError("look model rejected the frame") from error
    except urllib.error.URLError as error:
        print("[luma] look model unreachable")
        raise RuntimeError("look model unreachable") from error


def decode_jpeg(image_b64: str) -> bytes:
    cleaned = "".join((image_b64 or "").split())
    if cleaned.startswith("data:"):
        comma = cleaned.find(",")
        cleaned = cleaned[comma + 1 :] if comma >= 0 else ""
    if not cleaned:
        raise ValueError("The look was empty. Send a camera frame and try again.")
    if len(cleaned) > 2_000_000:
        raise ValueError("That look is too large. Try again.")
    try:
        raw = base64.b64decode(cleaned, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ValueError("The look could not be read. Try again.") from error
    if len(raw) < 32 or not raw.startswith(b"\xff\xd8"):
        raise ValueError("The look could not be read. Try again.")
    return raw


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    reasoning = message.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def ask_reason(jpeg: bytes) -> str:
    encoded = base64.b64encode(jpeg).decode("ascii")
    payload = {
        "model": COSMOS3_REASON_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": LOOK_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + encoded},
                    },
                ],
            }
        ],
        "max_tokens": 180,
        "temperature": 0,
    }
    data = _gpu_json(COSMOS3_REASON_URL + "/v1/chat/completions", payload, timeout=45)
    choices = data.get("choices") or []
    if not choices:
        return ""
    return message_text(choices[0].get("message") or {})


def ask_judge(description: str) -> str:
    """Ask the model to choose Clear, Wait, or Stop from the description alone."""
    payload = {
        "model": COSMOS3_REASON_MODEL,
        "messages": [
            {
                "role": "user",
                "content": JUDGE_PROMPT + (description or "The frame had no description."),
            }
        ],
        "max_tokens": 80,
        "temperature": 0,
    }
    data = _gpu_json(COSMOS3_REASON_URL + "/v1/chat/completions", payload, timeout=45)
    choices = data.get("choices") or []
    if not choices:
        return ""
    return message_text(choices[0].get("message") or {})


def parse_judge(text: str) -> tuple[str, str]:
    """Read the judge model's signal. This does not score the scene with a keyword list."""
    raw = text or ""
    signal = re.search(r"SIGNAL:\s*(clear|wait|stop)\b", raw, re.I)
    why = re.search(r"WHY:\s*(.+)", raw, re.I)
    reason = " ".join(why.group(1).split()) if why else ""
    if signal:
        return signal.group(1).lower(), reason or "the judge chose this from the description"
    lone = re.search(r"\b(clear|wait|stop)\b", raw, re.I)
    if lone:
        return lone.group(1).lower(), reason or "the judge chose this from the description"
    return "wait", "the judge did not return a signal"


def yolo_counts(video_b64: str) -> dict[str, int]:
    """Optional second signal. YOLO accepts an MP4 clip, not a still frame."""
    cleaned = "".join((video_b64 or "").split())
    if cleaned.startswith("data:"):
        comma = cleaned.find(",")
        cleaned = cleaned[comma + 1 :] if comma >= 0 else ""
    if len(cleaned) < 64 or len(cleaned) > 4_000_000:
        return {}
    try:
        raw = base64.b64decode(cleaned, validate=False)
    except (ValueError, base64.binascii.Error):
        return {}
    if len(raw) < 12 or raw[4:8] != b"ftyp":
        return {}
    try:
        data = _gpu_json(
            YOLO_URL + "/v1/infer",
            {
                "video_base64": cleaned,
                "filename": "look.mp4",
                "include_frames": False,
            },
            timeout=45,
        )
    except RuntimeError:
        return {}
    return parse_counts(data.get("object_counts"))


def parse_look(text: str) -> dict:
    raw = re.sub(
        r"\s+(PATH|PERSON|VEHICLE|SHARING|DISTANCE|SCENE):",
        r"\n\1:",
        text or "",
        flags=re.I,
    )
    parsed = {
        "path": "unsure",
        "person": None,
        "vehicle": None,
        "sharing": None,
        "distance": "",
        "scene": "",
    }
    patterns = {
        "path": re.compile(r"^PATH:\s*(open|approaching|blocked|unsure)\b", re.I),
        "person": re.compile(r"^PERSON:\s*(yes|no)\b", re.I),
        "vehicle": re.compile(r"^VEHICLE:\s*(yes|no)\b", re.I),
        "sharing": re.compile(r"^SHARING:\s*(yes|no)\b", re.I),
        "distance": re.compile(r"^DISTANCE:\s*(near|far)\b", re.I),
        "scene": re.compile(r"^SCENE:\s*(.+)$", re.I),
    }
    leftovers: list[str] = []
    for line in raw.splitlines():
        line = line.strip().lstrip("-* ")
        if not line:
            continue
        matched = False
        for key, pattern in patterns.items():
            match = pattern.match(line)
            if not match:
                continue
            matched = True
            value = match.group(1).strip()
            if key in ("person", "vehicle", "sharing"):
                parsed[key] = value.lower() == "yes"
            elif key in ("path", "distance"):
                parsed[key] = value.lower()
            else:
                parsed[key] = value
        if not matched:
            leftovers.append(line)
    if not parsed["scene"] and leftovers:
        parsed["scene"] = " ".join(leftovers)
    return parsed


def scene_has(scene: str, words: tuple[str, ...]) -> bool:
    lowered = scene.lower()
    return any(word in lowered for word in words)


def one_short_sentence(scene: str, limit: int = 15) -> str:
    """One spoken sentence, about 15 words, with no look disclaimer."""
    text = re.sub(r"[#*_`>]", "", scene or "")
    text = " ".join(text.split())
    text = re.sub(r"^(stop|wait|continue|clear)\b[.!:\-\s]*", "", text, flags=re.I).strip()
    text = re.sub(r"\blaptop camera\b", "camera", text, flags=re.I)
    if not text:
        return ""
    split = re.search(r"[.!?]", text)
    if split:
        text = text[: split.end()]
    lowered = text.lower()
    banned = (
        "cross now",
        "you should cross",
        "go ahead and cross",
        "step off the curb",
        "step off a curb",
        "enter the road",
        "enter the street",
        "walk into the road",
        "walk into the street",
        "this is only what",
        "cane",
        "sounding signal",
        "person with you",
        "person with them",
    )
    if any(phrase in lowered for phrase in banned):
        return ""
    words = text.replace(".", " ").replace("!", " ").replace("?", " ").split()
    words = [word.strip(",;:") for word in words if word.strip(",;:")]
    if len(words) > limit:
        text = " ".join(words[:limit])
    else:
        text = text.rstrip(".!?")
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def way_is_clear(scene: str) -> bool:
    return scene_has(
        scene,
        (
            "street is clear",
            "street looks clear",
            "clear street",
            "street ahead is clear",
            "looks open",
            "nothing blocking",
            "nothing in the way",
            "nothing in my way",
            "not in the way",
            "not in my way",
            "out of the way",
            "clear path",
            "open path",
            "empty path",
            "no obstacle",
            "path ahead is clear",
            "sidewalk is clear",
            "sidewalk is empty",
        ),
    )


def something_in_the_way(scene: str) -> bool:
    lowered = (scene or "").lower()
    if "nothing blocking" in lowered or "not blocking" in lowered or lowered.strip() in {"none", "none."}:
        return False
    return scene_has(
        scene,
        (
            "in the way",
            "in my way",
            "blocking",
            "blocks the",
            "standing in the path",
            "standing in the way",
            "on the path ahead",
            "directly ahead",
        ),
    )


def hazard_approaching(scene: str) -> bool:
    return scene_has(
        scene,
        (
            "approaching",
            "coming closer",
            "closing in",
            "heading toward",
            "heading towards",
            "moving toward",
            "moving towards",
            "driving toward",
            "driving towards",
            "walking toward",
            "walking towards",
            "toward the camera",
            "towards the camera",
            "toward you",
            "towards you",
            "oncoming",
            "getting closer",
        ),
    )


def off_to_the_side(scene: str) -> bool:
    return scene_has(
        scene,
        (
            "off to the side",
            "to the side",
            "on the side",
            "to the left",
            "to the right",
            "on the left",
            "on the right",
            "parked",
            "alongside",
        ),
    )


def decide_look(parsed: dict, counts: dict[str, int] | None = None) -> tuple[str, str]:
    """Stop, Wait, or Clear from the camera sentence, plus why that signal was chosen.

    The scene sentence decides. PATH is only a backup when that sentence does not
    say what is happening. Clear when the road is open or the thing is off to the side.
    Wait when a hazard is approaching. Stop when the description says something is
    already in the way. If a person or vehicle is described and the sentence does
    not say the road is open, Wait.
    """
    del counts
    path = parsed.get("path") or "unsure"
    scene = (parsed.get("scene") or "").strip()
    sharing = bool(parsed.get("sharing"))
    in_the_way = something_in_the_way(scene)
    approaching = hazard_approaching(scene)
    beside = off_to_the_side(scene) and not in_the_way and not approaching
    explicit_none = scene.lower().rstrip(".") in {"", "none"}

    if in_the_way:
        return "stop", "the camera sentence says something is already in the way"
    if sharing:
        return "stop", "the camera says the path is being shared"
    if approaching and not beside:
        return "wait", "the camera sentence says a hazard is approaching"
    if beside:
        return "clear", "the thing is off to the side"
    if way_is_clear(scene):
        return "clear", "the camera sentence says the way is open"
    if explicit_none:
        if path == "blocked":
            return "stop", "the camera marked the path blocked"
        if path == "approaching":
            return "wait", "the camera marked a hazard approaching"
        return "clear", "the camera sentence says nothing is ahead"
    if path == "open":
        return "clear", "the camera marked the path open"
    if path == "blocked":
        return "stop", "the camera marked the path blocked"
    if path == "approaching":
        return "wait", "the camera marked a hazard approaching"
    if parsed.get("person") or parsed.get("vehicle"):
        return "wait", "a person or vehicle is described and the way is not described as open"
    return "wait", "the camera sentence does not say the way is open"


BLOCKERS = (
    "person",
    "child",
    "dog",
    "bicycle",
    "bike",
    "scooter",
    "car",
    "bus",
    "truck",
    "van",
    "motorcycle",
    "tree",
    "pole",
    "bin",
    "sign",
    "wall",
    "door",
    "chair",
    "bag",
    "box",
)


def hazard_phrase(status: str, scene: str, parsed: dict) -> str:
    """Short phrase after Wait, Stop, or Clear. Names what is in the way."""
    if status == "clear":
        return "Nothing in the way."
    text = (scene or "").lower()
    if status == "wait" and hazard_approaching(scene):
        verb = "approaching"
    elif status == "stop":
        verb = "blocking"
    else:
        verb = "nearby"
    named = re.search(rf"\b(?:a|an)\s+([a-z]+)\s+{verb}\b", text)
    if named:
        thing = named.group(1)
    else:
        found = [(text.find(thing), thing) for thing in BLOCKERS if thing in text]
        found = [(index, thing) for index, thing in found if index >= 0]
        if found:
            thing = min(found)[1]
        elif parsed.get("vehicle"):
            thing = "car"
        elif parsed.get("person"):
            thing = "person"
        else:
            thing = "something"
    if thing == "something":
        return f"Something {verb}."
    article = "An" if thing[:1] in "aeiou" else "A"
    return f"{article} {thing} {verb}."


def camera_description(text: str) -> str:
    """Keep the model's own description, up to two short sentences."""
    cleaned = re.sub(r"[#*_`>]", "", text or "")
    cleaned = " ".join(cleaned.split())
    cleaned = re.sub(r"^(stop|wait|continue|clear)\b[.!:\-\s]*", "", cleaned, flags=re.I).strip()
    if not cleaned:
        return ""
    banned = (
        "cross now",
        "you should cross",
        "go ahead and cross",
        "step off the curb",
        "step off a curb",
        "enter the road",
        "enter the street",
        "walk into the road",
        "walk into the street",
    )
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    kept: list[str] = []
    for part in parts:
        if any(phrase in part.lower() for phrase in banned):
            continue
        kept.append(part if part[-1] in ".!?" else part + ".")
        if len(kept) == 2:
            break
    text = " ".join(kept)
    words = text.replace(".", " ").replace("!", " ").replace("?", " ").split()
    if len(words) > 40:
        text = " ".join(words[:40]).rstrip(".,;:") + "."
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def speak_description(status: str, description: str) -> str:
    """The signal, then what the camera described."""
    label = {"stop": "Stop.", "wait": "Wait.", "clear": "Clear."}[status]
    sentence = description or "The camera did not describe the frame."
    if sentence[-1] not in ".!?":
        sentence += "."
    return f"{label} {sentence}"


def look_trace(description: str, status: str, reason: str) -> str:
    scene = description or "none"
    return (
        f"Camera description: {scene} "
        f"Judge {status} because {reason}."
    )


def describe_look(image_b64: str, video_b64: str = "") -> dict:
    del video_b64
    jpeg = decode_jpeg(image_b64)
    text = ask_reason(jpeg)
    description = camera_description(text)
    status, reason = parse_judge(ask_judge(description))
    spoken = speak_description(status, description)
    trace = look_trace(description, status, reason)
    print(f"[luma] look {trace}")
    return {
        "status": status,
        "status_label": LOOK_LABELS[status],
        "spoken": spoken,
        "detail": trace,
        "place_label": "Camera",
        "question": "Look now",
        "moments": [],
    }


SAFE_SOURCE = re.compile(r"^s3://[A-Za-z0-9._:/-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "Luma/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[luma] {self.address_string()} {fmt % args}")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/index.html"):
            body = INDEX_HTML.encode("utf-8")
            self._bytes(200, "text/html; charset=utf-8", body)
            return
        if path == "/health":
            self._json(200, {"ok": True})
            return
        if path == "/api/catalog":
            self._json(200, catalog())
            return
        if path == "/api/media":
            self._media()
            return
        if path == "/api/walk":
            self._walk()
            return
        self._json(404, {"error": "Not found."})

    def _walk(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        place_id = (query.get("place") or [""])[0]
        try:
            self._json(200, walk_route(place_id))
        except ValueError as error:
            self._json(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            print(f"[luma] walk error: {type(error).__name__}: {error}")
            self._json(502, {"error": "The walk could not be loaded. Try again."})

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/look":
            self._look()
            return
        if path != "/api/guide":
            self._json(404, {"error": "Not found."})
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length > 20_000:
            self._json(413, {"error": "Question is too long."})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "Ask again in plain text."})
            return
        try:
            result = guide(
                str(payload.get("mode") or ""),
                str(payload.get("place") or ""),
                str(payload.get("question") or ""),
            )
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return
        except Exception as error:  # noqa: BLE001
            print(f"[luma] guide error: {type(error).__name__}: {error}")
            self._json(502, {"error": "The cameras could not be searched. Try again."})
            return
        self._json(200, result)

    def _look(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            self._json(400, {"error": "The look was empty. Send a camera frame and try again."})
            return
        if length > 3_000_000:
            self._json(413, {"error": "That look is too large. Try again."})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "The look could not be read. Try again."})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "The look could not be read. Try again."})
            return
        try:
            result = describe_look(
                str(payload.get("image_base64") or ""),
                str(payload.get("video_base64") or ""),
            )
        except ValueError as error:
            self._json(400, {"error": str(error)})
            return
        except Exception as error:  # noqa: BLE001
            print(f"[luma] look error: {type(error).__name__}")
            self._json(502, {"error": "Luma could not look at that frame. Try again."})
            return
        self._json(200, result)

    def _media(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        source = (query.get("source") or [""])[0]
        if not SAFE_SOURCE.match(source):
            self._json(400, {"error": "That clip cannot be played."})
            return
        try:
            upstream = CLIENT.open_stream(source, self.headers.get("Range"))
        except urllib.error.HTTPError as error:
            self._json(error.code, {"error": "The clip could not be played."})
            return
        except Exception as error:  # noqa: BLE001
            print(f"[luma] media error: {type(error).__name__}")
            self._json(502, {"error": "The clip could not be played."})
            return
        try:
            self.send_response(getattr(upstream, "status", 200))
            for header in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                value = upstream.headers.get(header)
                if value:
                    self.send_header(header, value)
            self.end_headers()
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            upstream.close()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._bytes(status, "application/json; charset=utf-8", body)

    def _bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[luma] listening on {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
