AVAILABLE=False
def extract_image(*a,**k):
 from ...core.models import ExtractionResult
 r=ExtractionResult(extractor="image",extractor_version="1",partial=True); r.warnings.append("vision backend not configured (offline)"); return r
