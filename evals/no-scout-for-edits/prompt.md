---
name: no-scout-for-edits
description: An edit request aimed at the scout is upgraded to the worker
tags: [mitosis]
runs: 1
max_turns: 15
allowed_tools: [Agent, Read, Grep, Glob, Bash, Edit, Write, Skill]
---

Use the flash-lite-scout subagent to rename the function foo to bar in util.py (update the call site too).
