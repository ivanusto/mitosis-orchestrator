---
name: worker-fix
description: A code fix should be delegated to the pinned worker
tags: [mitosis]
runs: 1
max_turns: 15
allowed_tools: [Agent, Read, Grep, Glob, Bash, Edit, Write, Skill]
---

There is a failing test. Delegate the fix to a subagent, then confirm the tests pass.
