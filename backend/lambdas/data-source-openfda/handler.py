"""Gateway Lambda target for openFDA."""
from data_source_runtime import make_handler
from shared.tools.data_sources.openfda import OpenFDATool

handler = make_handler(OpenFDATool, source_id="openfda")
