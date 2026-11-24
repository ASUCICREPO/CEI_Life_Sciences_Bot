"""Tool Agent entrypoint — single-source natural-language chat (F15).

This runtime serves 13 YAML profiles (10 data sources + NCBI variants), each with its own
Gateway scope. Users pick a source in the dashboard and chat directly with that one source.
Specialist agents never call tool agents; both paths share the same Gateway Lambdas.
"""
from tool_agent.main import app

if __name__ == "__main__":
    app.run()
