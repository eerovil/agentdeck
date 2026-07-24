"""Shared instructions for sessions started by AgentDeck."""

FILE_PRESENTATION_INSTRUCTIONS = """
When linking to files you created for the user:
- Use a relative .html or .htm path in a Markdown link only when the user should
  open it as a sandboxed HTML preview, for example [Open preview](report/index.html).
- Use an absolute filesystem path in a Markdown link when the user should open or
  download the file itself, for example [Download report](/tmp/report.pdf). This
  also applies to HTML files that should be downloaded instead of previewed.
Never use a relative path for a downloadable file.
""".strip()
