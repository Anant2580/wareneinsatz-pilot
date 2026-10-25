"""The two ways extraction can end without an invoice."""


class ExtractionError(Exception):
    """The file is broken, unsafe or not an invoice. It is stored with status 'failed' and the reason."""


class NeedsVisualExtraction(Exception):
    """The file has no machine-readable invoice data (scan, photo, plain PDF, ZUGFeRD without lines).

    It is stored with status 'received' and waits for the OCR + LLM extractor (step 5).
    """
