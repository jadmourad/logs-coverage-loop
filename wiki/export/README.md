# wiki/export/ — the raw snapshot

Put the raw Confluence content here, one file per page. `/wiki-ingest` reads
this folder and distils it into `../KNOWLEDGE_BASE.md`, which is the only wiki
file Worker B ever reads.

Two ways to fill it:

**Export by hand.** Confluence → Space tools → Content tools → Export, as HTML
or Markdown. Unpack it here. Filenames do not matter; readable ones help.

**Fetch through the Confluence MCP.** Run `/wiki-ingest` and name the space or
page tree. It fetches the pages and saves each one here as it goes.

Either way the snapshot is what makes the loop reproducible: Worker B makes
hundreds of judgements per run, and they should not depend on the network being
up or on a page being edited mid-run. Re-run `/wiki-ingest` when the wiki
changes.

Anything under this folder is input, never edited by the loop. Decisions a
human makes during `/coverage-escalations` are written to
`../KNOWLEDGE_BASE.md` and drafted for Confluence in `../pending-wiki-updates/`
— pushing them to the real wiki is a human action.
