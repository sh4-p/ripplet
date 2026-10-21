from __future__ import annotations
def export_report(title: str, sections: dict[str,str]) -> str:
    out=[f"# {title}",""]
    for k,v in sections.items(): out+= [f"## {k}","",v,""]
    return "\n".join(out)
