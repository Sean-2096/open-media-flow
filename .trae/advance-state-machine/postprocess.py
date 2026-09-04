import json, sys

src, dst = sys.argv[1], sys.argv[2]
scene = json.load(open(src, encoding="utf-8"))

def text_width(s, fs=16):
    w = 0
    for ch in s:
        w += fs if ord(ch) > 0x2E00 else int(fs * 0.56)
    return int(w) + 24

scene.setdefault("appState", {})["viewBackgroundColor"] = "#F7F6F5"

LEFT_TEXTS = {"t-title", "t-intro", "lg-t1", "lg-t2", "lg-t3", "lg-t4", "lg-t5"}
CHIP_IDS = {"lg-success", "lg-danger", "lg-warning", "lg-neutral"}

TEXT_FIXES = {
    "n-lip": "LIP_SYNCING 口型同步\nMuseTalk 口型 · 不过门禁降级旁白",
    "n-generated": "GENERATED 成片与审核\n轮询合成进度 · Pipeline.audit 自动审核",
    "n-failed": "AUTOMATION_FAILED（终态）\n重试次数耗尽",
    "n-partial": "PARTIAL_FAILURE（终态）\n部分平台发布失败",
}
LABEL_ANCHOR = {
    "a-bypass": (960, 1000),
    "a-rback": (225, 548),
    "a-pw2": (455, 720),
    "a-wresume": (390, 786),
}

def standalone_text(eid, text, cx, cy, fill="#0A0A0A"):
    longest = max(text.split("\n"), key=len)
    lines = text.count("\n") + 1
    w, h = text_width(longest), max(22, int(lines * 20 * 1.25) + 2)
    return {
        "id": eid,
        "type": "text",
        "x": cx - w / 2,
        "y": cy - h / 2,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": fill,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 0,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": 1,
        "version": 1,
        "versionNonce": 1,
        "isDeleted": False,
        "boundElements": [],
        "updated": 0,
        "link": None,
        "locked": False,
        "text": text,
        "fontSize": 16,
        "fontFamily": 2,
        "textAlign": "center",
        "verticalAlign": "middle",
        "originalText": text,
        "lineHeight": 1.25,
    }

extra = []
new_elements = []

for el in scene.get("elements", []):
    if el.get("type") in ("rectangle", "text", "arrow") and "strokeStyle" not in el:
        el["strokeStyle"] = "solid"
    if el.get("type") == "text" and el["id"] in LEFT_TEXTS:
        el["textAlign"] = "left"
    if el["id"] in CHIP_IDS:
        el["roundness"] = {"type": 3}

    label = el.get("label")
    if isinstance(label, dict) and label.get("text"):
        text = TEXT_FIXES.get(el["id"], label["text"])
        longest = max(text.split("\n"), key=len)
        lines = text.count("\n") + 1
        w, h = text_width(longest), max(24, int(lines * 20 * 1.25) + 6)

        if el.get("type") == "arrow":
            anchor = LABEL_ANCHOR.get(el["id"])
            if anchor is None:
                pts = el.get("points") or [[0, 0], [0, 0]]
                p0, p1 = pts[0], pts[-1]
                anchor = (el["x"] + (p0[0] + p1[0]) / 2, el["y"] + (p0[1] + p1[1]) / 2)
            lbl = standalone_text("lbl-" + el["id"], text, *anchor,
                                  fill="#404040")
            lbl["textAlign"] = "left"
            extra.append(lbl)
            el.pop("label")
        else:
            txt_id = "lbl-" + el["id"]
            cx = el["x"] + el["width"] / 2
            cy = el["y"] + el["height"] / 2
            t = standalone_text(txt_id, text, cx, cy)
            t["containerId"] = el["id"]
            extra.append(t)
            el.setdefault("boundElements", []).append({"type": "text", "id": txt_id})
            el.pop("label")

    new_elements.append(el)

scene["elements"] = new_elements + extra
json.dump(scene, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("elements:", len(scene["elements"]), "texts added:", len(extra))
