# Шаблоны заполняет только Codex

Null/NOT_RUN — не заглушки для пользователя, а запрет случайного live запуска с вымышленными значениями. Codex формирует реальный host manifest из inventory, model catalog, endpoint receipts, tools/list, существующего бюджета и native qualification. Пример coverage требует actual base SHA и необходимых MCP/tool names после discovery.

Host `mcp` содержит только фактически настроенные серверы:

```json
{
  "serena": {
    "config": {
      "type": "local",
      "command": ["<PINNED_NATIVE_EXECUTABLE>", "<VERIFIED_ARGS>", "{workspace}"],
      "codemode": false
    },
    "access": "read",
    "allowed_tools": ["<ACTUAL_TOOL_NAME_FROM_TOOLS_LIST>"],
    "tools_receipt": {"path": "<PRIVATE_ABSOLUTE_RECEIPT>", "sha256": "<ACTUAL_SHA256>"}
  }
}
```

Это shape illustration, не ready-to-run command. Конкретные flags проверяются по установленному релизу. Для каждого writing server дополнительно scope_enforced=true и реальный negative test. Remote credential headers только env references, private HTTP требует explicit authorized private network.
