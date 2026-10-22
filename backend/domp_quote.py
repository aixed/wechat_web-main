"""Read current quote text and its image reference without interpreting image text."""
from __future__ import annotations

import xml.etree.ElementTree as ET


def sql_quote(message):
    if str(message.get("msgtype") or "") != "49":
        return None
    try:
        root = ET.fromstring(str(message.get("msg") or ""))
    except ET.ParseError:
        return None
    app = root if root.tag == "appmsg" else root.find(".//appmsg")
    if app is None or app.findtext("type") != "57":
        return None
    ref = app.find("refermsg")
    image = None
    if ref is not None and ref.findtext("type") == "3":
        content = ref.findtext("content") or ""
        content_node = ref.find("content")
        if content_node is not None and len(content_node):
            content = "".join(ET.tostring(child, encoding="unicode") for child in content_node)
        image = {"id": ref.findtext("svrid") or "", "msgtype": "3", "msg": content,
                 "fromid": ref.findtext("fromusr") or "", "chat_id": ref.findtext("chatusr") or ""}
    return {"text": app.findtext("title") or "", "image": image}
