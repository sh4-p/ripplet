from __future__ import annotations
import xml.sax.saxutils as su
def export(store, path: str):
    nodes=store.all_nodes(); edges=store.all_edges()
    with open(path,"w",encoding="utf-8") as f:
        f.write('<?xml version="1.0"?>\n<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n<graph id="G" edgedefault="directed">\n')
        for n in nodes: f.write(f'<node id="{su.escape(n.id)}"><data key="label">{su.escape(n.label)}</data></node>\n')
        for e in edges: f.write(f'<edge source="{su.escape(e["source"])}" target="{su.escape(e["target"])}"><data key="relation">{su.escape(e["relation"])}</data></edge>\n')
        f.write('</graph>\n</graphml>\n')
    return path
