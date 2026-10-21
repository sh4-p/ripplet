"""PDF extraction requires optional deps (pypdf). Graceful fallback."""
AVAILABLE=False
def extract_pdf(*a,**k):
 from ...core.models import ExtractionResult
 r=ExtractionResult(extractor="pdf",extractor_version="1",partial=True); r.warnings.append("pdf backend not installed"); return r
