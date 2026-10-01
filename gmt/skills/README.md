# Skills — reusable roles and prompt packs

A **skill** is a role definition in a markdown file: the persona, the rules,
and worked examples. Tasks point at one by name.

```
gmt/tasks/t_classify_defect.py      gmt/skills/inspector.md
    skill = "inspector"      ---->    ## System / ## Guidelines / ## Examples
```

| skill           | tier | used by                              |
|-----------------|------|--------------------------------------|
| `inspector`     | 1    | classify_defect                      |
| `extractor`     | 2    | extract_defect_fields                |
| `analyst`       | 2    | text_to_sql                          |
| `narrator`      | 1    | answer_from_rows                     |
| `graph_builder` | 2    | extract_triples                      |
| `planner`       | 3    | designing new tasks (you + Claude)   |

## Editing a prompt

Edit the `.md`. That is the whole workflow — no python change, no restart
beyond the next run. Then **measure**:

```bash
python3 -m gmt bench --tiers 1,2
```

If accuracy did not move, revert. Prompt changes that feel better and
measure the same are how prompt files rot.

## Why the python `system` string still exists

Every task keeps a `system = "..."` fallback in its `.py`. If the skill file
is missing or misnamed, the task uses that instead of crashing. Prompts are
data; a missing data file should degrade, not take the line down.
