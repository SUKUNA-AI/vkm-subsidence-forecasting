# Источники и пределы проверки

Проверка документации: 14.11.2026. Это primary sources, а не обзоры/рейтинги. URLs — указатели на изменяемую документацию; для native приёмки Codex сохраняет фактическую версию/commit/hash релиза и нужные фрагменты docs/schema локально.

Исходные attached packages — первичный материал данного code review. `validation/input-packages.json` фиксирует хеши. Старые утверждения о capabilities/моделях не считаются автоматически подтверждёнными.

- OpenCode CLI/headless, standalone: https://opencode.ai/docs/cli/
- OpenCode CLI commands/options: https://opencode.ai/docs/cli/commands/
- OpenCode configuration/precedence/schema: https://opencode.ai/docs/config/
- OpenCode machine-readable schema: https://opencode.ai/config.json
- OpenCode agents/steps/request caveat: https://opencode.ai/docs/agents/
- OpenCode permissions: https://opencode.ai/docs/permissions/
- OpenCode policies: https://opencode.ai/docs/policies/
- OpenCode providers/request settings: https://opencode.ai/docs/providers/
- OpenCode skills: https://opencode.ai/docs/skills/
- OpenCode MCP transport/names/context: https://opencode.ai/docs/mcp-servers/
- OpenRouter limits/key metadata: https://openrouter.ai/docs/api/reference/limits
- OpenRouter provider routing: https://openrouter.ai/docs/guides/routing/provider-selection
- Graphify local implementation: https://github.com/Graphify-Labs/graphify
- Serena primary project/docs: https://github.com/oraios/serena
- Serena running guide: https://oraios.github.io/serena/02-usage/020_running.html
- ast-grep official MCP project: https://github.com/ast-grep/ast-grep-mcp
- Context7 official project: https://github.com/upstash/context7
- Codex skills official documentation: https://developers.openai.com/codex/skills/

Полная JSON Schema OpenCode не исполнялась валидатором в этой среде; проверены документированные поля и собственные contract tests. Полная schema-validation именно установленного релиза обязательна на WORKSTATION. Реальные OpenRouter model IDs не вписаны в templates: проверка конкретного аккаунта/endpoint не выполнялась.

Web docs подтверждают возможности/синтаксис, но не native работу установленных приложений пользователя. Официальное описание Graphify local/MCP не доказывает непосредственную совместимость с существующим scientific Neo4j. Фактические цифры тестов принадлежат нашей локальной проверке, не внешним источникам.
