# AGENTS.md

面向 AI agent 的仓库工作约定。

## Agent skills

### Issue tracker

Issue 记录在 Gitea（`luis/PrivLink`），通过 gitea-issues skill 调用
Gitea REST API 读写。见 `docs/agents/issue-tracker.md`。

### Domain docs

单上下文（single-context）布局：根目录 `CONTEXT.md` + `docs/adr/`。
见 `docs/agents/domain.md`。
