"""Gateway Lambda target for PatentsView."""
from data_source_runtime import make_handler
from shared.tools.data_sources.patentsview import PatentsViewTool

handler = make_handler(PatentsViewTool, source_id="patentsview")
