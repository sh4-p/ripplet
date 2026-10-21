AVAILABLE=False
def extract_office(*a,**k):
 from ...core.models import ExtractionResult
 r=ExtractionResult(extractor="office",extractor_version="1",partial=True); r.warnings.append("office backend not installed"); return r
