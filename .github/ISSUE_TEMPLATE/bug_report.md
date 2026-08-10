---
name: Bug report
about: Something is broken or behaves unexpectedly
title: "fix: "
labels: bug
---

**Component**
<!-- agentctl (CLI) / brain / pw-executor / control-api / orchestrator / store-gateway / docs
     The set follows from the directories under cmd/ plus brain/ and pw-executor/ — check there
     rather than trusting this line, and say what you observed if none of them fits. -->

**What happened**
<!-- Observed behaviour. Include the structured exit code (0/1/2/3) if relevant. -->

**Expected**

**Repro**
1. command / config (redact secrets):
2. target URL or fixture:
3. …

**Environment**
- commit SHA:
- run mode: explore / goal / describe / replay / baseline / chat / mcp-server
- transport: jsonrpc / mcp · store: local / grpc
- OS / Go / Python / Node versions:

**Logs / artifacts**
<!-- stderr (logs go to stderr, never stdout), runs/<id>/*, trace.zip. NEVER paste secrets or prompt content. -->
