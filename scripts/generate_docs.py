#!/usr/bin/env python3
import argparse
import html
import json
import re
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


def architecture_diagram_asset_id(diagram):
    return f"system-architecture-{diagram['id']}"


def architecture_d2_path(diagram):
    return f"diagrams/{architecture_diagram_asset_id(diagram)}.d2"


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
    risk_ids = {risk["id"] for risk in data.get("risks", [])}
    sequence_group_ids = {group["id"] for group in data.get("sequenceGroups", [])}

    for api in data["apis"]:
        if api["categoryId"] not in category_ids:
            raise ValueError(f"API {api['id']} references missing category {api['categoryId']}")

    for api_id in data["apiDetails"]:
        if api_id not in api_ids:
            raise ValueError(f"apiDetails references missing API {api_id}")
        for risk_id in data["apiDetails"][api_id].get("riskIds", []):
            if risk_id not in risk_ids:
                raise ValueError(f"apiDetails {api_id} references missing risk {risk_id}")

    for sequence in data["sequences"]:
        for risk_id in sequence.get("riskIds", []):
            if risk_id not in risk_ids:
                raise ValueError(f"Sequence {sequence['id']} references missing risk {risk_id}")
        if sequence_group_ids:
            if "groupId" not in sequence:
                raise ValueError(f"Sequence {sequence['id']} is missing groupId")
            if sequence["groupId"] not in sequence_group_ids:
                raise ValueError(f"Sequence {sequence['id']} references missing sequence group {sequence['groupId']}")
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

    if data.get("database"):
        collection_ids = {collection["id"] for collection in data["database"]["collections"]}
        for collection in data["database"]["collections"]:
            field_names = {field["name"] for field in collection["fields"]}
            for index in collection.get("indexes", []):
                index_label = index.get("name") or ", ".join(index["fields"])
                for field_name in index["fields"]:
                    if field_name not in field_names:
                        raise ValueError(
                            f"Collection {collection['id']} index {index_label} references missing field {field_name}"
                        )
            for risk_id in collection.get("riskIds", []):
                if risk_id not in risk_ids:
                    raise ValueError(f"Collection {collection['id']} references missing risk {risk_id}")
            for api_id in collection.get("relatedApis", []):
                if api_id not in api_ids:
                    raise ValueError(f"Collection {collection['id']} references missing API {api_id}")
        for access in data["database"].get("apiAccess", []):
            if access["apiId"] not in api_ids:
                raise ValueError(f"Database API access references missing API {access['apiId']}")
            for collection_id in access.get("reads", []) + access.get("writes", []):
                if collection_id not in collection_ids:
                    raise ValueError(f"Database API access {access['apiId']} references missing collection {collection_id}")
        for model in data["database"].get("stateModels", []):
            if model["collection"] not in collection_ids:
                raise ValueError(f"State model {model['id']} references missing collection {model['collection']}")

    if data.get("systemArchitecture"):
        architecture_diagram_ids = set()
        for diagram in data["systemArchitecture"].get("diagrams", []):
            if diagram["id"] in architecture_diagram_ids:
                raise ValueError(f"Architecture diagram {diagram['id']} is duplicated")
            architecture_diagram_ids.add(diagram["id"])
            node_ids = {node["id"] for node in diagram.get("nodes", [])}
            for edge in diagram.get("edges", []):
                if edge["from"] not in node_ids:
                    raise ValueError(
                        f"Architecture diagram {diagram['id']} edge references missing node {edge['from']}"
                    )
                if edge["to"] not in node_ids:
                    raise ValueError(
                        f"Architecture diagram {diagram['id']} edge references missing node {edge['to']}"
                    )

    sequence_ids = {sequence["id"] for sequence in data["sequences"]}
    collection_ids = {collection["id"] for collection in data.get("database", {}).get("collections", [])}
    for risk in data.get("risks", []):
        for api_id in risk.get("relatedApis", []):
            if api_id not in api_ids:
                raise ValueError(f"Risk {risk['id']} references missing API {api_id}")
        for sequence_id in risk.get("relatedSequences", []):
            if sequence_id not in sequence_ids:
                raise ValueError(f"Risk {risk['id']} references missing sequence {sequence_id}")
        for collection_id in risk.get("relatedCollections", []):
            if collection_id not in collection_ids:
                raise ValueError(f"Risk {risk['id']} references missing collection {collection_id}")


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
      --surface-2: #fbfcfe;
      --ink: #17202a;
      --muted: #526174;
      --line: #d8e0ea;
      --accent: #1c7c66;
      --blue: #2457a6;
      --blue-soft: #e8f3ff;
      --danger: #b42318;
      --soft: #e5f4ef;
      --warning: #9a5b00;
      --warning-soft: #fff5dd;
      --shadow: 0 12px 28px rgba(20, 35, 55, 0.07);
      --code-bg: #111827;
      --code-ink: #e5e7eb;
      --focus: #8b5cf6;
    }}
    * {{ box-sizing: border-box; }}
    html {{
      max-width: 100%;
      scroll-behavior: smooth;
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
      overflow-x: clip;
    }}
    body {{
      width: 100%;
      max-width: 100%;
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.58;
      -webkit-text-size-adjust: 100%;
      text-size-adjust: 100%;
      overflow-x: clip;
    }}
    .skip-link {{
      position: absolute;
      left: 16px;
      top: 12px;
      z-index: 10;
      transform: translateY(-140%);
      padding: 10px 12px;
      color: #ffffff;
      background: var(--ink);
      border-radius: 6px;
      transition: transform 160ms ease;
    }}
    .skip-link:focus {{ transform: translateY(0); }}
    header {{
      background:
        linear-gradient(135deg, rgba(232, 243, 255, 0.9), rgba(229, 244, 239, 0.76)),
        var(--surface);
      border-bottom: 1px solid var(--line);
    }}
    .wrap {{
      width: min(1180px, calc(100% - 36px));
      margin: 0 auto;
    }}
    .hero {{ padding: 46px 0 30px; }}
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
    .search-panel {{
      max-width: 760px;
      margin-top: 22px;
    }}
    .search-panel input {{
      width: 100%;
      min-height: 48px;
      padding: 0 16px;
      color: var(--ink);
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      font: inherit;
    }}
    .search-panel input::placeholder {{ color: #718096; }}
    .search-panel input:focus {{
      border-color: color-mix(in srgb, var(--accent), var(--line) 35%);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent), transparent 86%), var(--shadow);
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
      max-height: calc(100vh - 36px);
      padding: 14px;
      overflow-y: auto;
      overscroll-behavior: contain;
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
      cursor: pointer;
      transition: color 160ms ease, background-color 160ms ease;
    }}
    nav a:hover {{ color: var(--ink); background: var(--soft); }}
    nav a[aria-current="true"] {{
      color: var(--accent);
      background: var(--soft);
    }}
    a:focus-visible,
    button:focus-visible,
    input:focus-visible,
    summary:focus-visible {{
      outline: 3px solid var(--focus);
      outline: 3px solid color-mix(in srgb, var(--focus), transparent 45%);
      outline-offset: 2px;
    }}
    section {{
      margin-bottom: 24px;
      padding: 24px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      scroll-margin-top: 18px;
    }}
    h2 {{ margin: 0 0 16px; font-size: 24px; letter-spacing: 0; }}
    h3 {{ margin: 22px 0 8px; font-size: 18px; letter-spacing: 0; }}
    h4 {{ margin: 14px 0 8px; font-size: 16px; letter-spacing: 0; }}
    p {{ margin: 0 0 14px; }}
    a {{ color: var(--blue); font-weight: 800; text-decoration: none; text-underline-offset: 3px; }}
    a:hover {{ text-decoration: underline; }}
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
    .table-scroll {{
      width: 100%;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{ width: 100%; min-width: 680px; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 11px 12px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--line); }}
    th {{ color: var(--muted); background: var(--surface-2); font-weight: 800; }}
    tr:last-child td {{ border-bottom: 0; }}
    .nested-schema-row > td {{
      padding: 0;
      background: var(--surface-2);
    }}
    .nested-schema {{
      padding: 12px;
      border-top: 1px solid var(--line);
    }}
    .nested-schema-title {{
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 900;
    }}
    .nested-schema-table {{
      min-width: 560px;
      font-size: 13px;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: clip;
    }}
    .nested-schema-table th,
    .nested-schema-table td {{
      padding: 8px 10px;
      border-bottom: 1px solid var(--line);
    }}
    .nested-schema-table tr:last-child td {{
      border-bottom: 0;
    }}
    .top-links {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }}
    .top-links a {{
      display: inline-flex;
      align-items: center;
      min-height: 38px;
      padding: 7px 12px;
      color: var(--ink);
      background: rgba(255, 255, 255, 0.82);
      border: 1px solid var(--line);
      border-radius: 8px;
      cursor: pointer;
      transition: border-color 160ms ease, background-color 160ms ease;
    }}
    .top-links a:hover {{
      background: #ffffff;
      border-color: color-mix(in srgb, var(--blue), var(--line) 55%);
      text-decoration: none;
    }}
    .top-links a:active,
    nav a:active {{
      transform: translateY(1px);
    }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .sequence-group {{ margin-top: 20px; }}
    .sequence-group:first-of-type {{ margin-top: 0; }}
    .sequence-group > p {{ color: var(--muted); }}
    .card, .box {{
      padding: 16px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .card {{
      transition: border-color 160ms ease, box-shadow 160ms ease, background-color 160ms ease;
    }}
    .card:has(a):hover {{
      background: #ffffff;
      border-color: color-mix(in srgb, var(--blue), var(--line) 56%);
      box-shadow: 0 10px 22px rgba(20, 35, 55, 0.08);
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
    .status-planned {{
      color: var(--warning);
      background: var(--warning-soft);
    }}
    .note {{
      margin-top: 14px;
      padding: 13px 15px;
      color: #7a4a00;
      background: var(--warning-soft);
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
      background: var(--surface-2);
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
      background: var(--surface-2);
    }}
    .d2-details summary {{
      cursor: pointer;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 800;
    }}
    @media (min-width: 901px) {{
      .wrap {{
        width: min(1440px, calc(100% - 64px));
      }}
      .hero {{
        padding: 34px 0 26px;
      }}
      .hero .wrap {{
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(330px, 420px);
        gap: 16px 36px;
        align-items: center;
      }}
      .hero .eyebrow,
      .hero h1,
      .hero .lead {{
        grid-column: 1;
      }}
      .hero .search-panel,
      .hero .top-links {{
        grid-column: 2;
      }}
      .search-panel {{
        max-width: none;
        margin-top: 0;
      }}
      .top-links {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        margin-top: 0;
        gap: 8px;
      }}
      .top-links a {{
        justify-content: center;
        min-height: 38px;
        padding-inline: 10px;
        text-align: center;
      }}
      .layout {{
        grid-template-columns: 300px minmax(0, 1fr);
        gap: 28px;
      }}
      .layout > div {{
        min-width: 0;
      }}
      nav {{
        padding: 16px;
      }}
      nav a {{
        padding: 9px 10px;
      }}
      section {{
        padding: 28px;
      }}
      section > p,
      section > ol,
      section > ul {{
        max-width: 96ch;
      }}
      table {{
        font-size: 13.5px;
      }}
      th, td {{
        padding: 10px 11px;
      }}
      .card,
      .box {{
        padding: 18px;
      }}
      .grid {{
        gap: 16px;
      }}
    }}
    @media (min-width: 481px) and (max-width: 900px) {{
      .wrap {{
        width: min(100% - 24px, 1180px);
      }}
      .hero {{
        padding: 28px 0 22px;
      }}
      h1 {{ font-size: clamp(30px, 5.8vw, 40px); }}
      .lead {{
        max-width: 78ch;
        font-size: 16px;
      }}
      .search-panel {{
        max-width: 640px;
        margin-top: 18px;
      }}
      .top-links {{
        gap: 8px;
      }}
      .top-links a {{
        min-height: 36px;
        padding: 6px 10px;
        font-size: 14px;
      }}
      main {{ padding: 20px 0 42px; }}
      .layout {{
        grid-template-columns: minmax(170px, 210px) minmax(0, 1fr);
        gap: 16px;
      }}
      .layout > div {{
        min-width: 0;
      }}
      nav {{
        top: 12px;
        max-height: calc(100vh - 24px);
        padding: 10px;
      }}
      nav a {{
        padding: 7px 8px;
        font-size: 13px;
      }}
      section {{
        padding: 20px;
      }}
      h2 {{ font-size: 22px; }}
      h3 {{ font-size: 17px; }}
      table {{
        font-size: 13px;
      }}
      th, td {{
        padding: 9px 10px;
      }}
    }}
    @media (max-width: 480px) {{
      header {{
        width: 100%;
      }}
      .layout {{
        grid-template-columns: minmax(0, 1fr);
      }}
      .layout > nav,
      .layout > div {{
        min-width: 0;
        max-width: 100%;
      }}
      nav {{
        position: sticky;
        top: 0;
        z-index: 4;
        display: flex;
        gap: 8px;
        width: 100%;
        max-width: 100%;
        max-height: none;
        margin-inline: 0;
        padding: 10px 0;
        overflow-x: auto;
        overflow-y: hidden;
        border-radius: 0;
        box-shadow: 0 8px 18px rgba(20, 35, 55, 0.08);
        scrollbar-width: thin;
      }}
      nav a {{
        flex: 0 0 auto;
        min-height: 44px;
        max-width: min(72vw, 280px);
        display: inline-flex;
        align-items: center;
        padding: 8px 12px;
        background: var(--surface-2);
        border: 1px solid var(--line);
        white-space: normal;
      }}
      .grid {{ grid-template-columns: 1fr; }}
      .wrap {{
        width: calc(100% - 16px);
        max-width: 1180px;
      }}
      .hero {{
        width: 100%;
        padding: 34px 0 24px;
      }}
      h1 {{ font-size: clamp(27px, 7.4vw, 34px); }}
      h2 {{ font-size: 22px; }}
      h3 {{ font-size: 17px; }}
      h4 {{ font-size: 16px; }}
      p,
      li {{
        font-size: 16px;
        overflow-wrap: anywhere;
      }}
      .lead {{
        font-size: 16px;
        line-height: 1.56;
      }}
      .search-panel {{
        width: 100%;
        max-width: none;
        margin-top: 18px;
      }}
      .search-panel input {{ min-height: 46px; }}
      .top-links {{
        width: 100%;
        gap: 8px;
      }}
      .top-links a {{
        min-height: 44px;
        flex: 1 1 100%;
        min-width: 0;
        justify-content: center;
        text-align: center;
      }}
      main {{ padding: 18px 0 38px; }}
      section {{
        max-width: 100%;
        padding: 18px;
      }}
      section,
      .card,
      .box,
      .d2-diagram,
      .table-scroll {{
        border-radius: 8px;
      }}
      .card,
      .box {{ padding: 14px; }}
      .card ul,
      .box ul {{ padding-left: 20px; }}
      code {{
        overflow-wrap: anywhere;
        word-break: break-word;
      }}
      .table-scroll {{
        max-width: 100%;
        overflow-x: visible;
        border: 0;
        -webkit-overflow-scrolling: touch;
      }}
      table,
      table.has-many-columns {{
        width: 100%;
        min-width: 0;
        border-collapse: separate;
        border-spacing: 0;
        font-size: 13px;
      }}
      thead {{ display: none; }}
      tr,
      td {{
        display: block;
      }}
      tbody {{
        display: grid;
        gap: 8px;
      }}
      tr {{
        overflow: clip;
        border-radius: 8px;
        background: var(--surface);
        box-shadow: inset 0 0 0 1px var(--line);
      }}
      td {{
        display: grid;
        grid-template-columns: minmax(82px, 32%) minmax(0, 1fr);
        gap: 10px;
        align-items: start;
        padding: 9px 10px;
        border-bottom: 0;
        overflow-wrap: anywhere;
      }}
      td:not(:last-child) {{ border-bottom: 1px solid var(--line); }}
      td::before {{
        content: attr(data-label);
        color: var(--muted);
        font-size: 12px;
        font-weight: 800;
        line-height: 1.45;
      }}
      pre {{
        font-size: 12px;
        -webkit-overflow-scrolling: touch;
      }}
      .diagram {{
        margin-inline: -2px;
        -webkit-overflow-scrolling: touch;
      }}
      .d2-toolbar {{
        position: sticky;
        left: 0;
        justify-content: flex-start;
      }}
      .d2-toolbar a,
      .d2-details summary {{
        min-height: 44px;
        display: inline-flex;
        align-items: center;
      }}
      .d2-svg {{
        width: 900px;
        max-width: none;
      }}
      .d2-source {{ min-width: 760px; }}
    }}
    @media (max-width: 480px) {{
      body {{ line-height: 1.54; }}
      .wrap {{ width: calc(100% - 12px); }}
      .hero {{
        width: 100%;
        padding: 28px 0 20px;
      }}
      .eyebrow {{ font-size: 13px; }}
      h1 {{ font-size: clamp(25px, 8vw, 31px); }}
      h2 {{ font-size: 20px; }}
      h3 {{ font-size: 16px; }}
      p,
      li {{ font-size: 15px; }}
      section {{
        padding: 16px;
        margin-bottom: 16px;
      }}
      nav {{
        padding-inline: 0;
      }}
      nav a {{ max-width: 78vw; }}
      td {{
        grid-template-columns: minmax(72px, 30%) minmax(0, 1fr);
        gap: 8px;
        padding: 8px 9px;
      }}
      .d2-svg {{ width: 820px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      html {{ scroll-behavior: auto; }}
      *, *::before, *::after {{
        animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important;
        transition-duration: 0.01ms !important;
      }}
    }}
  </style>
  {extra_head}
</head>
<body>
<a class="skip-link" href="#content">본문으로 건너뛰기</a>
{body}
<script>
(() => {{
  const input = document.querySelector("[data-doc-search]");
  const targets = Array.from(document.querySelectorAll("main section, article.card, article.box"));
  const navLinks = Array.from(document.querySelectorAll("nav a[href^='#']"));

  if (input) {{
    input.addEventListener("input", () => {{
      const query = input.value.trim().toLowerCase();
      for (const target of targets) {{
        const text = target.textContent.toLowerCase();
        target.hidden = Boolean(query) && !text.includes(query);
      }}
    }});
  }}

  if ("IntersectionObserver" in window && navLinks.length) {{
    const linksById = new Map(navLinks.map((link) => [decodeURIComponent(link.hash.slice(1)), link]));
    const observer = new IntersectionObserver((entries) => {{
      const visible = entries
        .filter((entry) => entry.isIntersecting && !entry.target.hidden)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      for (const link of navLinks) link.removeAttribute("aria-current");
      const active = linksById.get(visible.target.id);
      if (active) active.setAttribute("aria-current", "true");
    }}, {{ rootMargin: "-18% 0px -68% 0px", threshold: [0.01, 0.18, 0.36] }});
    for (const section of document.querySelectorAll("main section[id]")) observer.observe(section);
  }}
}})();
</script>
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
  <div class="hero">
    <div class="wrap">
    <p class="eyebrow">{e(eyebrow)}</p>
    <h1>{e(title)}</h1>
    <p class="lead">{e(lead)}</p>
    <div class="search-panel">
      <input type="search" data-doc-search aria-label="문서 검색" placeholder="API, 상태, 컬렉션, 플로우 검색">
    </div>
    {link_html}
    </div>
  </div>
</header>
"""


def nav(items):
    return "<nav aria-label=\"문서 목차\">" + "".join(f"<a href=\"#{e(anchor)}\">{e(label)}</a>" for anchor, label in items) + "</nav>"


def table(html_table, columns=3):
    class_name = " class=\"has-many-columns\"" if columns >= 4 else ""
    headers = [re.sub(r"<[^>]+>", "", header).strip() for header in re.findall(r"<th[^>]*>(.*?)</th>", html_table, flags=re.S)]

    def label_cells(row_match):
        cells = re.findall(r"<td>(.*?)</td>", row_match.group(1), flags=re.S)
        if not cells or len(cells) > len(headers):
            return row_match.group(0)
        labelled_cells = "".join(
            f"<td data-label=\"{e(headers[index])}\">{cell}</td>"
            for index, cell in enumerate(cells)
        )
        return f"<tr>{labelled_cells}</tr>"

    labelled_table = re.sub(r"<tr>((?:<td>.*?</td>)+)</tr>", label_cells, html_table, flags=re.S)
    return f"<div class=\"table-scroll\">{labelled_table.replace('<table>', f'<table{class_name}>', 1)}</div>"


def format_db_subfield(field):
    parts = [field["name"]]
    if field.get("required"):
        parts.append("required")
    if field.get("enum"):
        parts.append("enum: " + "|".join(field["enum"]))
    return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else parts[0]


def format_db_field_details(field):
    details = []
    if field.get("ref"):
        details.append(f"ref: {field['ref']}")
    if field.get("enum"):
        details.append("enum: " + ", ".join(field["enum"]))
    if "example" in field:
        details.append("example: " + json.dumps(field["example"], ensure_ascii=False))
    if field.get("properties"):
        details.append("nested schema: properties")
    item_properties = field.get("items", {}).get("properties", [])
    if item_properties:
        details.append("nested schema: items.properties")
    return "; ".join(details) or "-"


def db_nested_schema(field):
    if field.get("properties"):
        return "properties", field["properties"]
    item_properties = field.get("items", {}).get("properties", [])
    if item_properties:
        return "items.properties", item_properties
    return None, []


def render_db_nested_schema(field):
    schema_kind, properties = db_nested_schema(field)
    if not properties:
        return ""
    rows = []
    for prop in properties:
        enum_value = ", ".join(prop.get("enum", [])) or "-"
        rows.append(
            "<tr>"
            f"<td class=\"nested-schema-cell\"><code>{e(prop['name'])}</code></td>"
            f"<td class=\"nested-schema-cell\"><code>{e(prop.get('type', '-'))}</code></td>"
            f"<td class=\"nested-schema-cell\">{'예' if prop.get('required') else '아니오'}</td>"
            f"<td class=\"nested-schema-cell\">{e(enum_value)}</td>"
            f"<td class=\"nested-schema-cell\">{e(prop.get('description', '-'))}</td>"
            "</tr>"
        )
    return (
        "<tr class=\"nested-schema-row\">"
        "<td colspan=\"5\">"
        "<div class=\"nested-schema\">"
        f"<p class=\"nested-schema-title\"><code>{e(field['name'])}</code> {e(schema_kind)}</p>"
        "<table class=\"nested-schema-table\">"
        "<thead><tr><th>하위 필드</th><th>타입</th><th>필수</th><th>Enum</th><th>설명</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
        "</div>"
        "</td>"
        "</tr>"
    )


def api_maps(data):
    apis = {api["id"]: api for api in data["apis"]}
    categories = {category["id"]: category for category in data["apiCategories"]}
    return apis, categories


def database_links(data, include_architecture=True):
    links = []
    architecture_ref = data["site"]["pages"].get("systemArchitecture")
    if include_architecture and architecture_ref and data.get("systemArchitecture"):
        links.append((architecture_ref["title"], f"./{architecture_ref['file']}"))
    page_ref = data["site"]["pages"].get("database")
    if page_ref:
        links.append((page_ref["title"], f"./{page_ref['file']}"))
    risk_ref = data["site"]["pages"].get("risks")
    if risk_ref and data.get("risks"):
        links.append((risk_ref["title"], f"./{risk_ref['file']}"))
    return links


def risk_map(data):
    return {risk["id"]: risk for risk in data.get("risks", [])}


def risk_links(data, risk_ids):
    if not risk_ids:
        return ""
    risks = risk_map(data)
    page_ref = data["site"]["pages"].get("risks")
    if not page_ref:
        return ""
    links = []
    for risk_id in risk_ids:
        risk = risks.get(risk_id)
        if risk:
            links.append(
                f"<li><a href=\"./{e(page_ref['file'])}#{e(risk_id)}\"><code>{e(risk_id)}</code></a> {e(risk['title'])}</li>"
            )
    if not links:
        return ""
    return f"""<div class="note">
  <strong>연관 잠재 위험</strong>
  <ul>{''.join(links)}</ul>
</div>"""


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
  <span class="status status-{e(seq['status'])}">{e(seq['status'])}</span>
  <h4>{title}</h4>
  <p>{e(seq['summary'])}</p>
  <ul>{api_lines}</ul>
</article>"""

    def grouped_cards(sequences):
        groups = sorted(data.get("sequenceGroups", []), key=lambda item: item["order"])
        if not groups:
            return f"<div class=\"grid\">{''.join(card(seq) for seq in sequences)}</div>"

        rendered_groups = []
        for group in groups:
            group_sequences = [seq for seq in sequences if seq.get("groupId") == group["id"]]
            if not group_sequences:
                continue
            description = f"<p>{e(group['description'])}</p>" if group.get("description") else ""
            rendered_groups.append(f"""<div class="sequence-group">
  <h3>{e(group['title'])}</h3>
  {description}
  <div class="grid">{''.join(card(seq) for seq in group_sequences)}</div>
</div>""")
        return "".join(rendered_groups)

    body = header(
        data["site"]["pages"]["sequenceIndex"]["title"],
        "Sequence Diagram Hub",
        "구독결제와 일반결제에서 필요한 시퀀스 다이어그램을 한 곳에서 찾는 허브입니다.",
        [("전체 API 목록", "./all-api-doc.html"), ("API 상세 설명", "./api-detail-doc.html")] + database_links(data)
    )
    body += f"""<main class="wrap" id="content">
  <section id="available">
    <h2>작성 완료</h2>
    {grouped_cards(available)}
  </section>
  <section id="planned">
    <h2>추가 예정</h2>
    {grouped_cards(planned)}
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
                detail = "<span class=\"status status-planned\">상세 예정</span>"
            rows.append(
                f"<tr id=\"{e(api['id'])}\"><td><code>{e(fmt_api(api))}</code></td><td>{e(api['role'])}</td><td>{detail}</td></tr>"
            )
        api_table = table(f"""<table>
    <thead><tr><th>API</th><th>역할</th><th>상세</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>""")
        sections.append(f"""<section id="{e(category['id'])}">
  <h2>{e(category['title'])}</h2>
  {api_table}
</section>""")

    body = header(
        data["site"]["pages"]["apiCatalog"]["title"],
        "API Catalog",
        "API의 전체 목록과 역할만 정리하는 색인입니다. 상세 계약은 API 상세 설명 페이지에서 관리합니다.",
        [("전체 시퀀스 목록", "./sequence-index.html"), ("API 상세 설명 단일 원본", "./api-detail-doc.html")] + database_links(data)
    )
    body += f"<main class=\"wrap layout\" id=\"content\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(data["site"]["pages"]["apiCatalog"]["title"], body)


def render_fields(title, fields):
    rows = "".join(
        f"<tr><td><code>{e(field['name'])}</code></td><td>{'예' if field['required'] else '아니오'}</td><td>{e(field['description'])}</td></tr>"
        for field in fields
    )
    return f"""<h3>{e(title)}</h3>
{table(f"<table><thead><tr><th>이름</th><th>필수</th><th>설명</th></tr></thead><tbody>{rows}</tbody></table>")}"""


def render_json_example(value):
    return f"<pre><code>{e(json.dumps(value, ensure_ascii=False, indent=2))}</code></pre>"


def render_failure_rules(rules):
    if not rules:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><code>{e(rule['code'])}</code></td>"
        f"<td>{e(rule['httpStatus'])}</td>"
        f"<td>{'예' if rule['retryable'] else '아니오'}</td>"
        f"<td>{e(rule['description'])}</td>"
        f"<td>{e(rule.get('resultState', '-'))}</td>"
        "</tr>"
        for rule in rules
    )
    return (
        "<h3>실패 규칙</h3>"
        + table(
            "<table><thead><tr><th>코드</th><th>HTTP</th><th>재시도</th><th>설명</th><th>결과 상태</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>",
            columns=5
        )
    )


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
            frontend_inputs = f"<h3>프론트 입력값</h3>{table(f'<table><thead><tr><th>값</th><th>출처</th><th>설명</th></tr></thead><tbody>{rows}</tbody></table>')}"
        body_fields = render_fields("Body 필드", request.get("bodyFields", [])) if request.get("bodyFields") else ""
        body_example = f"<h3>Body</h3>{render_json_example(request['bodyExample'])}" if "bodyExample" in request else "<h3>Body</h3><pre><code>요청 바디 없음</code></pre>"
        failure_rules = render_failure_rules(detail.get("failureRules", []))
        logic = "".join(f"<li>{e(item)}</li>" for item in detail["logic"])
        notes = "".join(f"<div class=\"note\">{e(item)}</div>" for item in detail.get("notes", []))
        section_parts = [
            f"<section id=\"{e(api['detailAnchor'])}\">",
            f"  <h2><code>{e(fmt_api(api))}</code></h2>",
            f"  <p>{e(detail['summary'])}</p>",
        ]
        if detail.get("redirectNote"):
            section_parts.append(f"  <div class=\"note\">{e(detail['redirectNote'])}</div>")
        risk_block = risk_links(data, detail.get("riskIds", []))
        if risk_block:
            section_parts.append(f"  {risk_block}")
        section_parts.extend([
            f"  {render_fields('Header', request.get('headers', []))}",
            f"  {render_fields('Cookie', request.get('cookies', []))}",
            f"  {body_example}",
        ])
        if body_fields:
            section_parts.append(f"  {body_fields}")
        if frontend_inputs:
            section_parts.append(f"  {frontend_inputs}")
        section_parts.extend([
            f"  {''.join(response_html)}",
            f"  {failure_rules}" if failure_rules else "",
            "  <h3>처리 로직</h3>",
            f"  <ol>{logic}</ol>",
        ])
        if notes:
            section_parts.append(f"  {notes}")
        section_parts.append("</section>")
        sections.append("\n".join(section_parts))

    body = header(
        data["site"]["pages"]["apiDetails"]["title"],
        "API Contract",
        "Header, Cookie, Body, Response, 처리 로직을 관리하는 API 계약의 단일 원본입니다.",
        [("전체 시퀀스 목록", "./sequence-index.html"), ("전체 API 목록", "./all-api-doc.html")] + database_links(data)
    )
    body += f"<main class=\"wrap layout\" id=\"content\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(data["site"]["pages"]["apiDetails"]["title"], body)


def render_database_doc(data):
    database = data["database"]
    apis, _ = api_maps(data)
    pages = data["site"]["pages"]
    nav_items = [("overview", "개요")]
    nav_items.extend((collection["id"], collection["title"]) for collection in database["collections"])
    if database.get("relationships"):
        nav_items.append(("relationships", "문서 관계"))
    if database.get("apiAccess"):
        nav_items.append(("api-access", "API별 읽기/쓰기"))
    if database.get("stateModels"):
        nav_items.append(("state-models", "상태 모델"))

    links = [
        ("전체 시퀀스 목록", "./sequence-index.html"),
        ("전체 API 목록", "./all-api-doc.html"),
        ("API 상세 설명", "./api-detail-doc.html"),
    ]
    body = header(
        pages["database"]["title"],
        "MongoDB Data Model",
        database.get("description") or "결제 시스템에서 사용하는 MongoDB 컬렉션, 관계, 상태, API 접근 방식을 정리합니다.",
        links
    )

    sections = [
        f"""<section id="overview">
  <h2>개요</h2>
  <p><strong>엔진:</strong> {e(database['engine'])}</p>
  <p>{e(database.get('description', '주요 컬렉션과 API별 데이터 변경 지점을 추적합니다.'))}</p>
</section>"""
    ]

    for collection in database["collections"]:
        field_rows = []
        for field in collection["fields"]:
            details = format_db_field_details(field)
            field_rows.append(
                "<tr>"
                f"<td><code>{e(field['name'])}</code></td>"
                f"<td><code>{e(field['type'])}</code></td>"
                f"<td>{'예' if field['required'] else '아니오'}</td>"
                f"<td>{e(field['description'])}</td>"
                f"<td>{e(details)}</td>"
                "</tr>"
            )
            nested_schema = render_db_nested_schema(field)
            if nested_schema:
                field_rows.append(nested_schema)
        index_rows = []
        for index in collection.get("indexes", []):
            partial_filter = "-"
            if "partialFilterExpression" in index:
                partial_filter = (
                    "partialFilterExpression: "
                    + json.dumps(index["partialFilterExpression"], ensure_ascii=False)
                )
            ttl_seconds = "-"
            if "expireAfterSeconds" in index:
                ttl_seconds = f"expireAfterSeconds: {index['expireAfterSeconds']}"
            index_rows.append(
                "<tr>"
                f"<td><code>{e(index.get('name', '-'))}</code></td>"
                f"<td><code>{e(', '.join(index.get('fields', [])))}</code></td>"
                f"<td>{'예' if index.get('unique') else '아니오'}</td>"
                f"<td>{'예' if index.get('sparse') else '아니오'}</td>"
                f"<td><code>{e(partial_filter)}</code></td>"
                f"<td><code>{e(ttl_seconds)}</code></td>"
                f"<td>{e(index.get('description', '-'))}</td>"
                "</tr>"
            )
        index_rows = "".join(index_rows)
        indexes = ""
        if index_rows:
            indexes = (
                "<h3>인덱스</h3>"
                + table(
                    "<table><thead><tr><th>이름</th><th>필드</th><th>유니크</th><th>Sparse</th>"
                    "<th>Partial Filter</th><th>TTL Seconds</th><th>설명</th></tr></thead>"
                    f"<tbody>{index_rows}</tbody></table>",
                    columns=7
                )
            )
        related_apis = "".join(
            f"<li><a href=\"./all-api-doc.html#{e(api_id)}\"><code>{e(fmt_api(apis[api_id]))}</code></a> - {e(apis[api_id]['role'])}</li>"
            for api_id in collection.get("relatedApis", []) if api_id in apis
        )
        api_block = f"<h3>관련 API</h3><ul>{related_apis}</ul>" if related_apis else ""
        risk_block = risk_links(data, collection.get("riskIds", []))
        fields_table = table(f"""<table>
    <thead><tr><th>필드</th><th>타입</th><th>필수</th><th>설명</th><th>상세</th></tr></thead>
    <tbody>{''.join(field_rows)}</tbody>
  </table>""", columns=5)
        sections.append(f"""<section id="{e(collection['id'])}">
  <h2>{e(collection['title'])} <code>{e(collection['name'])}</code></h2>
  <p>{e(collection['description'])}</p>
  {risk_block}
  <h3>필드</h3>
  {fields_table}
  {indexes}
  {api_block}
</section>""")

    if database.get("relationships"):
        rows = "".join(
            "<tr>"
            f"<td><code>{e(item['from'])}</code></td>"
            f"<td><code>{e(item['to'])}</code></td>"
            f"<td>{e(item['type'])}</td>"
            f"<td>{e(item['description'])}</td>"
            "</tr>"
            for item in database["relationships"]
        )
        sections.append(f"""<section id="relationships">
  <h2>문서 관계</h2>
  {table(f"<table><thead><tr><th>From</th><th>To</th><th>유형</th><th>설명</th></tr></thead><tbody>{rows}</tbody></table>", columns=4)}
</section>""")

    if database.get("apiAccess"):
        rows = "".join(
            "<tr>"
            f"<td><a href=\"./all-api-doc.html#{e(item['apiId'])}\"><code>{e(fmt_api(apis[item['apiId']]))}</code></a></td>"
            f"<td>{e(', '.join(item.get('reads', [])) or '-')}</td>"
            f"<td>{e(', '.join(item.get('writes', [])) or '-')}</td>"
            f"<td>{e(item['description'])}</td>"
            "</tr>"
            for item in database["apiAccess"]
        )
        sections.append(f"""<section id="api-access">
  <h2>API별 읽기/쓰기</h2>
  {table(f"<table><thead><tr><th>API</th><th>Read</th><th>Write</th><th>설명</th></tr></thead><tbody>{rows}</tbody></table>", columns=4)}
</section>""")

    if database.get("stateModels"):
        state_sections = []
        for model in database["stateModels"]:
            transitions = "".join(
                f"<tr><td><code>{e(item['from'])} → {e(item['to'])}</code></td><td>{e(item['event'])}</td></tr>"
                for item in model["transitions"]
            )
            state_sections.append(f"""<article class="box">
  <h3>{e(model['title'])}</h3>
  <p><code>{e(model['collection'])}.{e(model['field'])}</code></p>
  <p>{e(', '.join(model['states']))}</p>
  {table(f"<table><thead><tr><th>전이</th><th>이벤트</th></tr></thead><tbody>{transitions}</tbody></table>")}
</article>""")
        sections.append(f"""<section id="state-models">
  <h2>상태 모델</h2>
  <div class="grid">{''.join(state_sections)}</div>
</section>""")

    body += f"<main class=\"wrap layout\" id=\"content\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(pages["database"]["title"], body)


def render_risk_doc(data):
    page_ref = data["site"]["pages"]["risks"]
    apis, _ = api_maps(data)
    sequences = {sequence["id"]: sequence for sequence in data["sequences"]}
    collections = {
        collection["id"]: collection
        for collection in data.get("database", {}).get("collections", [])
    }
    nav_items = [(risk["id"], risk["title"]) for risk in data.get("risks", [])]
    sections = []
    for risk in data.get("risks", []):
        api_links = "".join(
            f"<li><a href=\"./api-detail-doc.html#{e(apis[api_id]['detailAnchor'])}\"><code>{e(fmt_api(apis[api_id]))}</code></a></li>"
            for api_id in risk.get("relatedApis", []) if api_id in apis and apis[api_id].get("detailAnchor")
        )
        sequence_links = "".join(
            f"<li><a href=\"./{e(sequences[sequence_id]['file'])}\">{e(sequences[sequence_id]['title'])}</a></li>"
            for sequence_id in risk.get("relatedSequences", []) if sequence_id in sequences
        )
        collection_links = "".join(
            f"<li><a href=\"./{e(data['site']['pages']['database']['file'])}#{e(collection_id)}\"><code>{e(collections[collection_id]['name'])}</code></a></li>"
            for collection_id in risk.get("relatedCollections", []) if collection_id in collections and data["site"]["pages"].get("database")
        )
        handling = "".join(f"<li>{e(item)}</li>" for item in risk["handling"])
        prevention = "".join(f"<li>{e(item)}</li>" for item in risk["prevention"])
        detection = f"<h3>탐지</h3><p>{e(risk['detection'])}</p>" if risk.get("detection") else ""
        related = "".join(
            part for part in [
                f"<h3>관련 API</h3><ul>{api_links}</ul>" if api_links else "",
                f"<h3>관련 플로우</h3><ul>{sequence_links}</ul>" if sequence_links else "",
                f"<h3>관련 DB 컬렉션</h3><ul>{collection_links}</ul>" if collection_links else "",
            ]
        )
        sections.append(f"""<section id="{e(risk['id'])}">
  <h2>{e(risk['title'])}</h2>
  <p><span class="status status-{e(risk['severity'])}">{e(risk['severity'])}</span> <code>{e(risk['category'])}</code></p>
  <p>{e(risk['summary'])}</p>
  <h3>발생 조건</h3>
  <p>{e(risk['trigger'])}</p>
  <h3>영향</h3>
  <p>{e(risk['impact'])}</p>
  {detection}
  <h3>처리 방안</h3>
  <ul>{handling}</ul>
  <h3>예방 장치</h3>
  <ul>{prevention}</ul>
  {related}
</section>""")

    body = header(
        page_ref["title"],
        "Potential Risk Register",
        "결제 플로우에서 실패, 재시도, 순서 역전, 동시 요청이 치명적인 결과로 이어질 수 있는 지점과 처리 방안을 정리합니다.",
        [("전체 시퀀스 목록", "./sequence-index.html"), ("전체 API 목록", "./all-api-doc.html"), ("API 상세 설명", "./api-detail-doc.html")] + database_links(data)
    )
    body += f"<main class=\"wrap layout\" id=\"content\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(page_ref["title"], body)


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


ARCHITECTURE_KIND_STYLES = {
    "client": ("#e8f3ff", THEME["client"]),
    "server": ("#eaf8f3", THEME["server"]),
    "scheduler": ("#fff8e6", THEME["scheduler"]),
    "database": ("#f1f5f9", THEME["admin"]),
    "queue": ("#fff5dd", THEME["scheduler"]),
    "worker": ("#eaf8f3", THEME["server"]),
    "provider": ("#f1edff", THEME["provider"]),
    "external": ("#f8fafc", THEME["neutral"]),
    "success": ("#e5f4ef", THEME["success"]),
    "failure": ("#fff1f0", THEME["failure"]),
}


def architecture_node_id(node_id):
    return re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def render_architecture_d2_diagram(diagram):
    lines = [
        "direction: right",
        f"title: {d2_quote(diagram['title'])}",
        "",
    ]
    for node in diagram["nodes"]:
        node_id = architecture_node_id(node["id"])
        fill, stroke = ARCHITECTURE_KIND_STYLES.get(
            node["kind"],
            ARCHITECTURE_KIND_STYLES["external"],
        )
        lines.append(f"{node_id}: {d2_quote(node['label'])} {{")
        lines.append("  shape: rectangle")
        lines.append(f"  style.fill: {d2_quote(fill)}")
        lines.append(f"  style.stroke: {d2_quote(stroke)}")
        lines.append("  style.border-radius: 6")
        lines.append("}")
    lines.append("")
    for edge in diagram["edges"]:
        from_id = architecture_node_id(edge["from"])
        to_id = architecture_node_id(edge["to"])
        lines.append(f"{from_id} -> {to_id}: {d2_quote(edge['label'])}")
    return "\n".join(lines) + "\n"


def render_d2_source_block(source_href, asset_id, title, source, rendered_d2_ids=None):
    rendered_d2_ids = rendered_d2_ids or set()
    if asset_id in rendered_d2_ids:
        return f"""<div class="d2-diagram">
  <div class="d2-toolbar"><a href="{e(source_href)}">D2 원본</a></div>
  <img class="d2-svg" src="diagrams/{e(asset_id)}.svg" alt="{e(title)}">
  <details class="d2-details">
    <summary>D2 원본 보기</summary>
    <pre class="d2-source"><code>{e(source)}</code></pre>
  </details>
</div>"""
    return f"""<div class="d2-diagram">
  <div class="d2-toolbar"><a href="{e(source_href)}">D2 원본</a></div>
  <pre class="d2-source"><code>{e(source)}</code></pre>
</div>"""


def render_d2_block(sequence, diagram, actors_by_id, apis, rendered_d2_ids=None):
    source = render_d2_diagram(diagram, actors_by_id, apis)
    source_href = d2_path(sequence, diagram)
    asset_id = diagram_asset_id(sequence, diagram)
    return render_d2_source_block(
        source_href,
        asset_id,
        diagram["title"],
        source,
        rendered_d2_ids,
    )


def render_architecture_d2_block(diagram, rendered_d2_ids=None):
    source = render_architecture_d2_diagram(diagram)
    source_href = architecture_d2_path(diagram)
    asset_id = architecture_diagram_asset_id(diagram)
    return render_d2_source_block(
        source_href,
        asset_id,
        diagram["title"],
        source,
        rendered_d2_ids,
    )


def render_system_architecture_doc(data, rendered_d2_ids=None):
    architecture = data["systemArchitecture"]
    page_ref = data["site"]["pages"]["systemArchitecture"]
    nav_items = [("overview", "개요")]
    nav_items.extend((diagram["id"], diagram["title"]) for diagram in architecture["diagrams"])
    nav_items.extend(
        [
            ("components", "컴포넌트 책임"),
            ("data-stores", "데이터 소유권"),
            ("operations", "운영과 실패 처리"),
        ]
    )
    sections = [
        f"""<section id="overview">
  <h2>개요</h2>
  <p>{e(architecture['summary'])}</p>
  <div class="note">이메일 실패는 결제, 인보이스, 구독, 감사 로그 상태를 롤백하지 않습니다. 발송 실패는 outbox 상태와 worker summary로 추적합니다.</div>
</section>"""
    ]
    for diagram in architecture["diagrams"]:
        description = f"<p>{e(diagram['description'])}</p>" if diagram.get("description") else ""
        sections.append(f"""<section id="{e(diagram['id'])}">
  <h2>{e(diagram['title'])}</h2>
  {description}
  <div class="diagram">{render_architecture_d2_block(diagram, rendered_d2_ids)}</div>
</section>""")

    component_rows = "".join(
        "<tr>"
        f"<td>{e(component['name'])}</td>"
        f"<td>{e(component['responsibility'])}</td>"
        f"<td>{e(component['failurePolicy'])}</td>"
        "</tr>"
        for component in architecture["components"]
    )
    sections.append(f"""<section id="components">
  <h2>컴포넌트 책임</h2>
  {table("<table><thead><tr><th>컴포넌트</th><th>책임</th><th>실패 정책</th></tr></thead>"
         f"<tbody>{component_rows}</tbody></table>", columns=3)}
</section>""")

    data_store_rows = "".join(
        "<tr>"
        f"<td><code>{e(store['name'])}</code></td>"
        f"<td>{e(store['owner'])}</td>"
        f"<td>{e(', '.join(store['keys']))}</td>"
        f"<td>{e(store['notes'])}</td>"
        "</tr>"
        for store in architecture["dataStores"]
    )
    sections.append(f"""<section id="data-stores">
  <h2>데이터 소유권</h2>
  {table("<table><thead><tr><th>저장소</th><th>소유 포트</th><th>주요 키/인덱스</th><th>비고</th></tr></thead>"
         f"<tbody>{data_store_rows}</tbody></table>", columns=4)}
</section>""")

    operation_items = "".join(
        f"<article class=\"box\"><h3>{e(operation['title'])}</h3><p>{e(operation['description'])}</p></article>"
        for operation in architecture["operations"]
    )
    sections.append(f"""<section id="operations">
  <h2>운영과 실패 처리</h2>
  <div class="grid">{operation_items}</div>
</section>""")

    body = header(
        page_ref["title"],
        "Email Notification Architecture",
        architecture["summary"],
        [
            ("전체 시퀀스 목록", "./sequence-index.html"),
            ("전체 API 목록", "./all-api-doc.html"),
            ("API 상세 설명", "./api-detail-doc.html"),
        ] + database_links(data, include_architecture=False),
    )
    body += f"<main class=\"wrap layout\" id=\"content\">{nav(nav_items)}<div>{''.join(sections)}</div></main>"
    return page(page_ref["title"], body)


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
  {table(f"<table><thead><tr><th>시점</th><th>구독 상태</th><th>결제 상태</th><th>설명</th></tr></thead><tbody>{''.join(state_summary)}</tbody></table>", columns=4)}
</section>"""
    risk_section = risk_links(data, sequence.get("riskIds", []))

    body = header(
        sequence["title"],
        "Sequence Diagram",
        sequence["summary"],
        [("전체 시퀀스 목록", "./sequence-index.html"), ("전체 API 목록", "./all-api-doc.html"), ("API 상세 설명", "./api-detail-doc.html")] + database_links(data)
    )
    body += f"""<main class="wrap" id="content">
  <section>
    <h2>사용 API</h2>
    <div class="grid">{api_links}</div>
  </section>
  {risk_section}
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
    out_dir = Path(out_dir)
    if isinstance(data_path, dict):
        data = data_path
    else:
        data_path = Path(data_path)
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
    if data.get("systemArchitecture"):
        for diagram in data["systemArchitecture"].get("diagrams", []):
            d2_file = out_dir / architecture_d2_path(diagram)
            d2_file.write_text(render_architecture_d2_diagram(diagram), encoding="utf-8")
            d2_files.append(d2_file)

    rendered_ids = {path.stem for path in diagrams_dir.glob("*.svg")}
    rendered_ids |= set(rendered_d2_ids or [])
    if render_d2:
        rendered_ids |= render_d2_svgs(d2_files)

    rendered = {
        pages["sequenceIndex"]["file"]: render_sequence_index(data),
        pages["apiCatalog"]["file"]: render_api_catalog(data),
        pages["apiDetails"]["file"]: render_api_details(data)
    }
    if data.get("database") and pages.get("database"):
        rendered[pages["database"]["file"]] = render_database_doc(data)
    if data.get("risks") and pages.get("risks"):
        rendered[pages["risks"]["file"]] = render_risk_doc(data)
    if data.get("systemArchitecture") and pages.get("systemArchitecture"):
        rendered[pages["systemArchitecture"]["file"]] = render_system_architecture_doc(data, rendered_ids)
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
