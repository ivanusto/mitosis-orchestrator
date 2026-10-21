---
name: orchestrate-skill
description: The orchestrate skill is invoked and delegates scouting
tags: [mitosis]
runs: 1
max_turns: 15
allowed_tools: [Agent, Read, Grep, Glob, Bash, Edit, Write, Skill]
---

Use the orchestrate skill to add a `__version__ = "0.1.0"` line to pkg/__init__.py. Scout the package layout first.
