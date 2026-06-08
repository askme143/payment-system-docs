#!/usr/bin/env python3
import argparse
import html
import json
import shutil
import subprocess
from pathlib import Path


THEME = {
    "client": "#2457a6",
    "server": "#1c7c66",
    "toss": "#7b4bc4",
    "provider": "#7b4bc4",
    "scheduler": "#9a5b00",
    "admin": "#334155",
    "system": "#334155",
    "success": "#1c7c66",
    "failure": "#b42318",
    "neutral": "#65758a"
}

ACTOR_FILLS = {
    "client": "#e8f3ff",
    "server": "#eaf8f3",
    "toss": "#f1edff",
    "provider": "#f1edff",
    "scheduler": "#fff8e6",
    "admin": "#f1f5f9",
    "system": "#f1f5f9",
    "neutral": "#f8fafc"
}


def e(value):
    return html.escape(str(value), quote=True)


def fmt_api(api):
    return f"{api['method']} {api['path']}"


def diagram_asset_id(sequence, diagram):
    return f"{sequence['id']}-{diagram['id']}"


def d2_path(sequence, diagram):
    return f"diagrams/{diagram_asset_id(sequence, diagram)}.d2"


def d2_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def d2_markdown_block(value):
    return "|md\n" + str(value).replace("|", "\\|") + "\n|"


def text_lines(*values):
    return [str(value) for value in values if value]


def is_uri_line(value):
    text = str(value).strip()
    if text.startswith("/"):
        return True
    parts = text.split(maxsplit=1)
    return len(parts) == 2 and parts[0] in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} and parts[1].startswith("/")


def message_label(label, code=None, note=None):
    rendered_code = f"**{code}**" if code and is_uri_line(code) else code
    return "\n\n".join(text_lines(label, rendered_code, note))


def note_label(label, code=None, note=None):
    return "\n".join(text_lines(label, code, note))


def step_code(step, apis):
    if step.get("apiId") in apis:
        return fmt_api(apis[step["apiId"]])
    if step.get("externalCall"):
        ext = step["externalCall"]
        return f"{ext['method']} {ext['path']}"
    return step.get("code")


def append_d2_message(lines, indent, from_id, to_id, label, stroke, markdown=False):
    prefix = " " * indent
    rendered_label = d2_markdown_block(label) if markdown else d2_quote(label)
    lines.append(f"{prefix}{from_id} -> {to_id}: {rendered_label} {{")
    lines.append(f"{prefix}  style.stroke: {d2_quote(stroke)}")
    lines.append(f"{prefix}  style.font-color: \"#334155\"")
    lines.append(f"{prefix}  style.font-size: 18")
    lines.append(f"{prefix}  style.italic: false")
    lines.append(f"{prefix}}}")


def render_d2_diagram(diagram, actors_by_id, apis):
    lines = [
        "diagram: {",
        "  shape: sequence_diagram",
        f"  label: {d2_quote(diagram['title'])}",
        "",
    ]
    for actor_id in diagram["actorIds"]:
        actor = actors_by_id[actor_id]
        lines.append(f"  {actor_id}: {d2_quote(actor['label'])}")
        lines.append(f"  {actor_id}.style.fill: {d2_quote(actor_fill(actor))}")
        lines.append(f"  {actor_id}.style.stroke: {d2_quote(actor_stroke(actor))}")
    lines.append("")

    for idx, step in enumerate(diagram["steps"], start=1):
        code = step_code(step, apis)
        if step["type"] == "message":
            label = message_label(step["label"], code, step.get("note"))
            to_actor = actors_by_id[step["to"]]
            append_d2_message(lines, 2, step["from"], step["to"], label, actor_stroke(to_actor), markdown=True)
        elif step["type"] == "self":
            actor_id = step["from"]
            lines.append(f"  {actor_id}.note_{idx}: {d2_quote(note_label(step['label'], code, step.get('note')))}")
        else:
            actor_id = step.get("from") or ("server" if "server" in diagram["actorIds"] else diagram["actorIds"][0])
            message = code or step["label"]
            lines.append(f"  {actor_id}.note_{idx}: {d2_quote(note_label(step['label'], message, step.get('note')))}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def load_data(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_data(data):
    actor_ids = {actor["id"] for actor in data["actors"]}
    category_ids = {category["id"] for category in data["apiCategories"]}
    api_ids = {api["id"] for api in data["apis"]}

    for api in data["apis"]:
        if api["categoryId"] not in category_ids:
            raise ValueError(f"API {api['id']} references missing category {api['categoryId']}")

    for api_id in data["apiDetails"]:
        if api_id not in api_ids:
            raise ValueError(f"apiDetails references missing API {api_id}")

    for sequence in data["sequences"]:
        for api_id in sequence.get("apiIds", []):
            if api_id not in api_ids:
                raise ValueError(f"Sequence {sequence['id']} references missing API {api_id}")
        for actor_id in sequence.get("actorIds", []):
            if actor_id not in actor_ids:
                raise ValueError(f"Sequence {sequence['id']} references missing actor {actor_id}")
        for diagram in sequence.get("diagrams", []):
            for actor_id in diagram.get("actorIds", []):
                if actor_id not in actor_ids:
                    raise ValueError(f"Diagram {diagram['id']} references missing actor {actor_id}")
            for api_id in diagram.get("relatedApiIds", []):
                if api_id not in api_ids:
                    raise ValueError(f"Diagram {diagram['id']} references missing API {api_id}")
            for step in diagram.get("steps", []):
                for key in ("from", "to"):
                    if key in step and step[key] not in actor_ids:
                        raise ValueError(f"Step {step['label']} references missing actor {step[key]}")
                if "apiId" in step and step["apiId"] not in api_ids:
                    raise ValueError(f"Step {step['label']} references missing API {step['apiId']}")


def page(title, body, extra_head=""):
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)}</title>
  <style>
    :root {{
      --bg: #f5f7fb;
      --surface: #ffffff;
      --ink: #17202a;
      --muted: #65758a;
      --line: #d8e0ea;
      --accent: #1c7c66;
      --blue: #2457a6;
      --danger: #b42318;
      --soft: #e5f4ef;
      --shadow: 0 14px 34px rgba(20, 35, 55, 0.08);
      --code-bg: #111827;
      --code-ink: #e5e7eb;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.58;
    }}
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }}
    .wrap {{
      width: min(1180px, calc(100% - 36px));
      margin: 0 auto;
    }}
    .hero {{ padding: 42px 0 28px; }}
    .eyebrow {{
      margin: 0 0 10px;
      color: var(--accent);
      font-size: 14px;
      font-weight: 800;
    }}
    h1 {{
      margin: 0;
      font-size: clamp(30px, 5vw, 48px);
      line-height: 1.14;
      letter-spacing: 0;
    }}
    .lead {{
      max-width: 900px;
      margin: 16px 0 0;
      color: var(--muted);
      font-size: 18px;
    }}
    main {{ padding: 28px 0 54px; }}
    .layout {{
      display: grid;
      grid-template-columns: 260px 1fr;
      gap: 24px;
    }}
    nav {{
      position: sticky;
      top: 18px;
      align-self: start;
      padding: 14px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    nav a {{
      display: block;
      padding: 8px 10px;
      color: var(--muted);
      text-decoration: none;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 700;
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    nav a:hover {{ color: var(--ink); background: var(--soft); }}
    section {{
      margin-bottom: 24px;
      padding: 24px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    h2 {{ margin: 0 0 16px; font-size: 24px; letter-spacing: 0; }}
    h3 {{ margin: 22px 0 8px; font-size: 18px; letter-spacing: 0; }}
    p {{ margin: 0 0 14px; }}
    a {{ color: var(--blue); font-weight: 800; text-decoration: none; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.95em; }}
    pre {{
      margin: 10px 0 0;
      padding: 14px;
      overflow-x: auto;
      color: var(--code-ink);
      background: var(--code-bg);
      border-radius: 8px;
      font-size: 13px;
      line-height: 1.5;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 11px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); background: #fbfcfe; font-weight: 800; }}
    .top-links {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 16px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .card, .box {{
      padding: 16px;
      background: #fbfcfe;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 8px;
      border-radius: 999px;
      color: var(--accent);
      background: var(--soft);
      font-size: 12px;
      font-weight: 900;
    }}
    .note {{
      margin-top: 14px;
      padding: 13px 15px;
      color: #7a4a00;
      background: #fff5dd;
      border: 1px solid #eed39a;
      border-radius: 8px;
    }}
    .diagram {{ overflow-x: auto; padding-bottom: 4px; }}
    .d2-diagram {{
      overflow-x: auto;
      padding: 0;
      background: #ffffff;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .d2-toolbar {{
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }}
    .d2-toolbar a {{
      font-size: 13px;
    }}
    .d2-source {{
      margin: 0;
      border-radius: 0 0 8px 8px;
    }}
    .d2-svg {{
      display: block;
      max-width: 100%;
      height: auto;
      margin: 0 auto;
      background: #ffffff;
    }}
    .d2-details {{
      border-top: 1px solid var(--line);
      background: #fbfcfe;
    }}
    .d2-details summary {{
      cursor: pointer;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
    }}
    @media (max-width: 900px) {{
      .layout {{ grid-template-columns: 1fr; }}
      nav {{ position: static; }}
      .grid {{ grid-template-columns: 1fr; }}
      .wrap {{ width: min(100% - 24px, 1180px); }}
    }}
  </style>
  {extra_head}
</head>
<body>
{body}
</body>
</html>
"""


def header(title, eyebrow, lead, links=None):
    link_html = ""
    if links:
        link_html = "<p class=\"top-links\">" + "".join(
            f"<a href=\"{e(href)}\">{e(label)}</a>" for label, href in links
        ) + "</p>"
    return f"""<header>
  <div class="wrap hero">
    <p class="eyebrow">{e(eyebrow)}</p>
    <h1>{e(title)}</h1>
    <p class="lead">{e(lead)}</p>
    {link_html}
  </div>
</header>
"""


def nav(items):
    return "<nav>" + "".join(f"<a href=\"#{e(anchor)}\">{e(label)}</a>" for anchor, label in items) + "</nav>"


def api_maps(data):
    apis = {api["id"]: api for api in data["apis"]}
    categories = {category["id"]: category for category in data["apiCategories"]}
    return apis, categories


def render_sequence_index(data):
    available = [seq for seq in data["sequences"] if seq["status"] == "available"]
    planned = [seq for seq in data["sequences"] if seq["status"] == "planned"]
    apis, _ = api_maps(data)

    def card(seq):
        api_lines = "".join(
            f"<li><code>{e(fmt_api(apis[api_id]))}</code></li>" for api_id in seq.get("apiIds", []) if api_id in apis
        )
        title = f"<a href=\"./{e(seq['file'])}\">{e(seq['title'])}</a>" if seq["status"] == "available" else e(seq["title"])
        return f"""<article class="card">
  <span class="status">{e(seq['status'])}</span>
  <h3>{title}</h3>
  <p>{e(seq['summary'])}</p>
  <ul>{api_lines}</ul>
</article>"""

    body = header(
        data["site"]["pages"]["sequenceIndex"]["title"],
        "Sequence Diagram Hub",
        "구독결제와 일반결제에서 필요한 시퀀스 다이어그램을 한 곳에서 찾는 허브입니다.",
        [("전체 API 목록", "./all-api-doc.html"), ("API 상세 설명", "./api-detail-doc.html")]
    )
    body += f"""<main class="wrap">
  <section id="available">
    <h2>작성 완료</h2>
    <div class="grid">{''.join(card(seq) for seq in available)}</div>
  </section>
  <section id="planned">
    <h2>추가 예정</h2>
    <div class="grid">{''.join(card(seq) for seq in planned)}</div>
  </section>
</main>"""
    return page(data["site"]["pages"]["sequenceIndex"]["title"], body)


def render_api_catalog(data):
    apis, _ = api_maps(data)
    categories = sorted(data["apiCategories"], key=lambda item: item["order"])
    nav_items = [(category["id"], category["title"]) for category in categories]
    sections = []
    for category in categories:
        rows = []
        for api in data["apis"]:
            if api["categoryId"] != category["id"]:
                continue
            if api["detailStatus"] == "available":
                detail = f"<a href=\"./api-detail-doc.html#{e(api['detailAnchor'])}\">상세 보기</a>"
            else:
                detail = "<span class=\"status\">상세 예정</span>"
            rows.append(
                f"<tr id=\"{e(api['id'])}\"><td><code>{e(fmt_api(api))}</code></td><td>{e(api['role'])}</td><td>{detail}</td></tr>"
            )
        sections.append(f"""<section id="{e(category['id'])}">
  <h2>{e(category['title'])}</h2>
  <table>
    <thead><tr><th>API</th><th>역할</th><th>상세</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>""")

    body = header(
        data["site"]["pages"]["apiCatalog"]["title"],
        "API Catalog",
        "API의 전체 목록과 역할만 정리하는 색인입니다. 상세 계약은 API 상세 설명 페이지에서 관리합니다.",
        [("전체 시퀀스 목록", "./sequence-index.html"), ("API 상세 설명 단일 원본", "./api-detail-doc.html")]
    )
    body += f"<main class=\"wrap layout\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(data["site"]["pages"]["apiCatalog"]["title"], body)


def render_fields(title, fields):
    rows = "".join(
        f"<tr><td><code>{e(field['name'])}</code></td><td>{'예' if field['required'] else '아니오'}</td><td>{e(field['description'])}</td></tr>"
        for field in fields
    )
    return f"""<h3>{e(title)}</h3>
<table><thead><tr><th>이름</th><th>필수</th><th>설명</th></tr></thead><tbody>{rows}</tbody></table>"""


def render_json_example(value):
    return f"<pre><code>{e(json.dumps(value, ensure_ascii=False, indent=2))}</code></pre>"


def render_api_details(data):
    apis, _ = api_maps(data)
    detailed_api_ids = [api["id"] for api in data["apis"] if api["detailStatus"] == "available" and api["id"] in data["apiDetails"]]
    nav_items = [(apis[api_id]["detailAnchor"], fmt_api(apis[api_id])) for api_id in detailed_api_ids]
    sections = []

    for api_id in detailed_api_ids:
        api = apis[api_id]
        detail = data["apiDetails"][api_id]
        request = detail["request"]
        response_html = []
        for response in detail["responses"]:
            example = render_json_example(response["bodyExample"]) if "bodyExample" in response else ""
            response_html.append(f"<h3>Response {response['status']}</h3><p>{e(response['description'])}</p>{example}")
        frontend_inputs = ""
        if detail.get("frontendInputs"):
            rows = "".join(
                f"<tr><td><code>{e(item['name'])}</code></td><td>{e(item['source'])}</td><td>{e(item['description'])}</td></tr>"
                for item in detail["frontendInputs"]
            )
            frontend_inputs = f"<h3>프론트 입력값</h3><table><thead><tr><th>값</th><th>출처</th><th>설명</th></tr></thead><tbody>{rows}</tbody></table>"
        body_fields = render_fields("Body 필드", request.get("bodyFields", [])) if request.get("bodyFields") else ""
        body_example = f"<h3>Body</h3>{render_json_example(request['bodyExample'])}" if "bodyExample" in request else "<h3>Body</h3><pre><code>요청 바디 없음</code></pre>"
        logic = "".join(f"<li>{e(item)}</li>" for item in detail["logic"])
        notes = "".join(f"<div class=\"note\">{e(item)}</div>" for item in detail.get("notes", []))
        sections.append(f"""<section id="{e(api['detailAnchor'])}">
  <h2><code>{e(fmt_api(api))}</code></h2>
  <p>{e(detail['summary'])}</p>
  {f'<div class="note">{e(detail["redirectNote"])}</div>' if detail.get("redirectNote") else ''}
  {render_fields("Header", request.get("headers", []))}
  {render_fields("Cookie", request.get("cookies", []))}
  {body_example}
  {body_fields}
  {frontend_inputs}
  {''.join(response_html)}
  <h3>처리 로직</h3>
  <ol>{logic}</ol>
  {notes}
</section>""")

    body = header(
        data["site"]["pages"]["apiDetails"]["title"],
        "API Contract",
        "Header, Cookie, Body, Response, 처리 로직을 관리하는 API 계약의 단일 원본입니다.",
        [("전체 시퀀스 목록", "./sequence-index.html"), ("전체 API 목록", "./all-api-doc.html")]
    )
    body += f"<main class=\"wrap layout\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(data["site"]["pages"]["apiDetails"]["title"], body)


def actor_theme(actor):
    return actor.get("theme") or actor.get("kind") or "neutral"


def actor_stroke(actor):
    return THEME.get(actor_theme(actor), THEME["neutral"])


def actor_fill(actor):
    return ACTOR_FILLS.get(actor_theme(actor), ACTOR_FILLS["neutral"])


def render_diagram(diagram, actors_by_id, apis):
    actor_ids = diagram["actorIds"]
    width = 1060
    top = 30
    actor_w = 230
    actor_h = 68
    step_gap = 132
    height = 170 + max(1, len(diagram["steps"])) * step_gap
    x_positions = {}
    if len(actor_ids) == 1:
        x_positions[actor_ids[0]] = width // 2
    else:
        margin = 120
        spread = width - margin * 2
        for idx, actor_id in enumerate(actor_ids):
            x_positions[actor_id] = int(margin + (spread * idx / (len(actor_ids) - 1)))

    marker_defs = []
    for name, color in THEME.items():
        marker_defs.append(
            f"<marker id=\"arrow-{name}\" viewBox=\"0 0 12 12\" refX=\"10\" refY=\"6\" markerWidth=\"9\" markerHeight=\"9\" orient=\"auto-start-reverse\"><path d=\"M 1 1 L 11 6 L 1 11 z\" fill=\"{color}\"></path></marker>"
        )

    parts = [
        f"<svg class=\"sequence-svg\" viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"{e(diagram['title'])}\">",
        "<defs>" + "".join(marker_defs) + "</defs>"
    ]
    for actor_id in actor_ids:
        actor = actors_by_id[actor_id]
        x = x_positions[actor_id]
        color = THEME.get(actor_theme(actor), THEME["neutral"])
        parts.append(f"<rect x=\"{x - actor_w // 2}\" y=\"{top}\" width=\"{actor_w}\" height=\"{actor_h}\" rx=\"8\" fill=\"#ffffff\" stroke=\"{color}\" stroke-width=\"2\"></rect>")
        parts.append(f"<text x=\"{x}\" y=\"{top + 28}\" text-anchor=\"middle\" class=\"actor-title\">{e(actor['label'])}</text>")
        parts.append(f"<text x=\"{x}\" y=\"{top + 50}\" text-anchor=\"middle\" class=\"actor-subtitle\">{e(actor['subtitle'])}</text>")
        parts.append(f"<line x1=\"{x}\" y1=\"{top + actor_h}\" x2=\"{x}\" y2=\"{height - 30}\" class=\"lifeline\"></line>")

    for idx, step in enumerate(diagram["steps"]):
        y = 145 + idx * step_gap
        theme = step.get("theme")
        if not theme:
            if step["type"] == "branch":
                theme = "failure"
            elif step.get("to") == "toss" or step.get("externalCall", {}).get("provider") == "tosspayments":
                theme = "toss"
            elif step.get("to") == "server":
                theme = "server"
            elif step.get("to") == "client":
                theme = "client"
            else:
                theme = "neutral"
        color = THEME.get(theme, THEME["neutral"])
        marker = f"url(#arrow-{theme if theme in THEME else 'neutral'})"
        label_x = 80
        code = step.get("code")
        if step.get("apiId") in apis:
            code = fmt_api(apis[step["apiId"]])
        elif step.get("externalCall"):
            ext = step["externalCall"]
            code = f"{ext['method']} {ext['path']}"
        if step["type"] == "message":
            x1 = x_positions[step["from"]]
            x2 = x_positions[step["to"]]
            label_x = min(x1, x2) + 20
            parts.append(f"<line x1=\"{x1}\" y1=\"{y}\" x2=\"{x2}\" y2=\"{y}\" class=\"msg-line\" stroke=\"{color}\" marker-end=\"{marker}\"></line>")
            label_text, _ = render_svg_text(label_x, y - 24, "msg-label", step["label"], 54, line_height=18)
            parts.append(label_text)
            if code:
                code_text, code_lines = render_svg_text(label_x, y + 28, "msg-code", code, 62, fill=color, line_height=20)
                parts.append(code_text)
                note_y = y + 54 + max(0, code_lines - 1) * 20
            else:
                note_y = y + 54
            if step.get("note"):
                note_text, _ = render_svg_text(label_x, note_y, "msg-note", step["note"], 72, line_height=17)
                parts.append(note_text)
        elif step["type"] == "self":
            x1 = x_positions[step["from"]]
            box_width = 360
            box_x = max(30, min(width - box_width - 30, x1 - box_width // 2))
            label_x = box_x + 20
            box_y = y - 44
            text_parts = []
            cursor_y = y - 20
            label_text, label_lines = render_svg_text(label_x, cursor_y, "msg-label", step["label"], 36, line_height=18)
            text_parts.append(label_text)
            cursor_y += label_lines * 18
            if code:
                cursor_y += 14
                code_text, code_lines = render_svg_text(label_x, cursor_y, "msg-code", code, 39, fill=color, line_height=20)
                text_parts.append(code_text)
                cursor_y += code_lines * 20
            if step.get("note"):
                cursor_y += 12
                note_text, note_lines = render_svg_text(label_x, cursor_y, "msg-note", step["note"], 48, line_height=17)
                text_parts.append(note_text)
                cursor_y += note_lines * 17
            box_height = max(104, cursor_y - box_y + 20)
            parts.append(f"<rect x=\"{box_x}\" y=\"{box_y}\" width=\"{box_width}\" height=\"{box_height}\" rx=\"8\" fill=\"#fbfcfe\" stroke=\"{color}\"></rect>")
            parts.extend(text_parts)
        else:
            box_width = 520
            x = (width - box_width) // 2
            label_x = x + 20
            fill = "#fff1f0" if theme == "failure" else "#fbfcfe"
            box_y = y - 44
            text_parts = []
            cursor_y = y - 20
            label_text, label_lines = render_svg_text(label_x, cursor_y, "msg-label", step["label"], 54, line_height=18)
            text_parts.append(label_text)
            cursor_y += label_lines * 18
            if code:
                cursor_y += 14
                code_text, code_lines = render_svg_text(label_x, cursor_y, "msg-code", code, 59, fill=color, line_height=20)
                text_parts.append(code_text)
                cursor_y += code_lines * 20
            if step.get("note"):
                cursor_y += 12
                note_text, note_lines = render_svg_text(label_x, cursor_y, "msg-note", step["note"], 72, line_height=17)
                text_parts.append(note_text)
                cursor_y += note_lines * 17
            box_height = max(104, cursor_y - box_y + 20)
            parts.append(f"<rect x=\"{x}\" y=\"{box_y}\" width=\"{box_width}\" height=\"{box_height}\" rx=\"8\" fill=\"{fill}\" stroke=\"{color}\" stroke-dasharray=\"7 6\"></rect>")
            parts.extend(text_parts)
    parts.append("</svg>")
    return "".join(parts)


render_diagram = render_d2_diagram


def render_d2_block(sequence, diagram, actors_by_id, apis, rendered_d2_ids=None):
    rendered_d2_ids = rendered_d2_ids or set()
    source = render_d2_diagram(diagram, actors_by_id, apis)
    source_href = d2_path(sequence, diagram)
    asset_id = diagram_asset_id(sequence, diagram)
    if asset_id in rendered_d2_ids:
        return f"""<div class="d2-diagram">
  <div class="d2-toolbar"><a href="{e(source_href)}">D2 원본</a></div>
  <img class="d2-svg" src="diagrams/{e(asset_id)}.svg" alt="{e(diagram["title"])}">
  <details class="d2-details">
    <summary>D2 원본 보기</summary>
    <pre class="d2-source"><code>{e(source)}</code></pre>
  </details>
</div>"""
    return f"""<div class="d2-diagram">
  <div class="d2-toolbar"><a href="{e(source_href)}">D2 원본</a></div>
  <pre class="d2-source"><code>{e(source)}</code></pre>
</div>"""


def render_sequence_page(data, sequence, rendered_d2_ids=None):
    apis, _ = api_maps(data)
    actors_by_id = {actor["id"]: actor for actor in data["actors"]}
    api_links = "".join(
        f"<div class=\"box\"><a href=\"./all-api-doc.html#{e(api_id)}\"><code>{e(fmt_api(apis[api_id]))}</code></a><p>{e(apis[api_id]['role'])}</p></div>"
        for api_id in sequence.get("apiIds", []) if api_id in apis
    )
    diagrams = []
    for diagram in sequence.get("diagrams", []):
        diagrams.append(f"""<section id="{e(diagram['id'])}">
  <h2>{e(diagram['title'])}</h2>
  {f'<p>{e(diagram["description"])}</p>' if diagram.get("description") else ''}
  <div class="diagram">{render_d2_block(sequence, diagram, actors_by_id, apis, rendered_d2_ids)}</div>
</section>""")
    state_summary = []
    for diagram in sequence.get("diagrams", []):
        for row in diagram.get("stateSummary", []):
            state_summary.append(
                f"<tr><td>{e(row['event'])}</td><td><code>{e(row['subscriptionState'])}</code></td><td><code>{e(row['paymentState'])}</code></td><td>{e(row['description'])}</td></tr>"
            )
    state_section = ""
    if state_summary:
        state_section = f"""<section id="states">
  <h2>상태 요약</h2>
  <table><thead><tr><th>시점</th><th>구독 상태</th><th>결제 상태</th><th>설명</th></tr></thead><tbody>{''.join(state_summary)}</tbody></table>
</section>"""

    body = header(
        sequence["title"],
        "Sequence Diagram",
        sequence["summary"],
        [("전체 시퀀스 목록", "./sequence-index.html"), ("전체 API 목록", "./all-api-doc.html"), ("API 상세 설명", "./api-detail-doc.html")]
    )
    body += f"""<main class="wrap">
  <section>
    <h2>사용 API</h2>
    <div class="grid">{api_links}</div>
  </section>
  {''.join(diagrams)}
  {state_section}
</main>"""
    return page(sequence["title"], body)


def render_d2_svgs(d2_files):
    d2 = shutil.which("d2")
    if not d2 or not d2_files:
        return set()
    rendered = set()
    for path in d2_files:
        output = path.with_suffix(".svg")
        subprocess.run([d2, "--layout", "dagre", "--theme", "4", str(path), str(output)], check=True)
        if output.exists():
            rendered.add(path.stem)
    return rendered


def generate_docs(data_path, out_dir, render_d2=False, rendered_d2_ids=None):
    data_path = Path(data_path)
    out_dir = Path(out_dir)
    data = load_data(data_path)
    validate_data(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    diagrams_dir = out_dir / "diagrams"
    diagrams_dir.mkdir(parents=True, exist_ok=True)

    outputs = []
    pages = data["site"]["pages"]
    d2_files = []
    actors_by_id = {actor["id"]: actor for actor in data["actors"]}
    apis, _ = api_maps(data)
    for sequence in data["sequences"]:
        if sequence["status"] == "available":
            for diagram in sequence.get("diagrams", []):
                d2_file = out_dir / d2_path(sequence, diagram)
                d2_file.write_text(render_d2_diagram(diagram, actors_by_id, apis), encoding="utf-8")
                d2_files.append(d2_file)

    rendered_ids = set(rendered_d2_ids or [])
    if render_d2:
        rendered_ids |= render_d2_svgs(d2_files)

    rendered = {
        pages["sequenceIndex"]["file"]: render_sequence_index(data),
        pages["apiCatalog"]["file"]: render_api_catalog(data),
        pages["apiDetails"]["file"]: render_api_details(data)
    }
    for sequence in data["sequences"]:
        if sequence["status"] == "available":
            rendered[sequence["file"]] = render_sequence_page(data, sequence, rendered_ids)

    for filename, content in rendered.items():
        path = out_dir / filename
        path.write_text(content, encoding="utf-8")
        outputs.append(path)
    return outputs


def main():
    parser = argparse.ArgumentParser(description="Generate payment documentation HTML from JSON data.")
    parser.add_argument("--data", default="docs-data/documentation.json", help="Path to documentation JSON.")
    parser.add_argument("--out", default=".", help="Output directory for generated HTML files.")
    parser.add_argument("--render-d2", action="store_true", help="Render generated D2 files to SVG when d2 is available.")
    args = parser.parse_args()
    generated = generate_docs(args.data, args.out, render_d2=args.render_d2)
    for path in generated:
        print(path)


if __name__ == "__main__":
    main()
