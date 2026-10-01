"""
Pipelines -- tasks composed into workflows.

| module               | what it does                        | your goal        |
|----------------------|-------------------------------------|------------------|
| p_qa_ingest          | messy notes -> ground truth + KG    | chatbot, KG,     |
|                      |                                     | business analytics|
| p_defect_inspection  | camera frames -> gate -> CNN -> DB  | fabric defect CV |
| p_analytics_ask      | question -> SQL -> grounded answer  | analytics, chat  |

A pipeline owns: ordering, branching, and the projection into gold tables.
A pipeline does NOT own: prompts, model choice, retries. Those belong to
the task and the router respectively.
"""
