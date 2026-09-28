"""VKM Corpus MCP (task §34, CP-19): thin MCP servers over the VKM API — never a database shell.

* ``vkm-corpus`` (:func:`~vkm_corpus.mcp.servers.build_read_server`) — read tools; holds only the API *read* token;
* ``vkm-corpus-admin`` (:func:`~vkm_corpus.mcp.servers.build_admin_server`) — plan-first reprocessing; holds the API
  *write* token and is not connected by default;
* :mod:`~vkm_corpus.mcp.http` — streamable HTTP (stateless, JSON) behind a bearer token and a host allow-list;
* :mod:`~vkm_corpus.mcp.acceptance` — scripted MCP client of the acceptance scenario §60 (JSON receipt).

The MCP layer has no database drivers: every tool is one or two API calls, and every result is the API's
``ApiResponse`` with its ``vkm.envelope/1`` objects.
"""
