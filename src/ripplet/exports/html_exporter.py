from __future__ import annotations
import html
def export(nodes, edges, path: str):
    with open(path,"w",encoding="utf-8") as f:
        f.write("<html><head><meta charset='utf-8'><title>Ripplet</title></head><body>")
        f.write(f"<h1>Graph: {len(nodes)} nodes, {len(edges)} edges</h1><ul>")
        for n in nodes[:500]: f.write(f"<li>{html.escape(n.label)} <small>{html.escape(n.id)}</small></li>")
        f.write("</ul></body></html>")
    return path
