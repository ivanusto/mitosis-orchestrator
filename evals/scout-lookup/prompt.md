---
name: scout-lookup
description: Read-only lookup should be delegated to the pinned scout
tags: [mitosis]
runs: 1
max_turns: 15
allowed_tools: [Agent, Read, Grep, Glob, Bash, Edit, Write, Skill]
---

Use a subagent to find where parse_token is defined and which files import it; report file paths.
