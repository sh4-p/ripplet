AVAILABLE=False
def extract_transcript(*a,**k):
 from ...core.models import ExtractionResult
 r=ExtractionResult(extractor="transcript",extractor_version="1",partial=True); r.warnings.append("transcription backend not configured"); return r
