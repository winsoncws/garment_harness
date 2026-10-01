# 07 — Roadmap: your seven initiatives on one harness

Every item on your list maps onto the same five primitives. That is the
argument for building the harness first: the second initiative costs a
fraction of the first.

---

## Coverage today

| # | Your initiative | Status | Where it lives |
|---|-----------------|--------|----------------|
| 1 | Camera vision — fabric defect detection | **shell ready, needs weights** | `t_frame_gate`, `t_defect_cnn`, `p_defect_inspection` |
| 2 | Delegated CV — OpenCV gate → GPU CNN | **working** | `p_defect_inspection` |
| 3 | Internal chatbot | **analytics half working**, RAG half designed | `p_analytics_ask` |
| 4 | Supply chain analytics | **schema + query path ready**, needs ERP feed | `store`, `text_to_sql` |
| 5 | Business analytics | **working** | `p_analytics_ask`, `metric_daily` |
| 6 | Market analysis | designed, not built | below |
| 7 | Knowledge graph | **working** | `t_extract_triples`, `kg_node`/`kg_edge` |
| 8 | Fabric physics (wish) | designed, not built | below |

---

## 1–2. Vision — what is actually left

The pipeline, the gate, the ROI cropping, the storage, the review flagging
and the provenance are done. What is missing is a trained model, and the
work to get one is data work, not code work:

1. **Collect.** Point a camera at a loom. Save frames that pass the gate.
   `p_defect_inspection` already stores the ROI it found — use those crops.
2. **Label.** 200–500 crops per class is enough for a first pass with a
   pretrained backbone. Prioritise the classes that cost money, not the ones
   that are common.
3. **Train.** Fine-tune a ResNet-18 or EfficientNet-B0. Anything bigger is
   premature before you know your per-class accuracy.
4. **Export TorchScript** → `torch.jit.script(model).save("defect_v1.pt")`.
5. `export GMT_CNN_MODEL=/models/defect_v1.pt` and bump `DefectCNN.version`.

**The active-learning loop is already wired.** Predictions below
`review_below` set `needs_review = 1`. Those are the frames nearest the
decision boundary — the most informative labels you can buy. Label those
first, retrain, repeat.

**Tune the gate before you tune the model.** `DEFAULTS` in `t_frame_gate.py`
are starting points for a 1080p top-lit camera. Run the gate over a day of
footage, look at `gate_reasons`, and adjust until you drop 99%+ of frames
without dropping any defect. Every 1% you shave off the gate is 1% more GPU
for the cameras you have not installed yet.

---

## 3. Chatbot — you already tried RAG; here is where it fits

The team's RAG experiment probably underperformed for a structural reason:
**retrieval cannot count.** "How many oil marks on LOOM-07 last week" is not
a retrieval question, and no amount of chunking fixes that.

The fix is to route:

```
                    question
                        │
                        ▼
             route_question  (tier 1, ~0.2s)
                  │         │
        structured│         │unstructured
                  ▼         ▼
          p_analytics_ask   RAG over SOPs, manuals,
          (text_to_sql)     supplier emails, spec sheets
                  │         │
                  └────┬────┘
                       ▼
              answer_from_rows / cite sources
```

**Next task to write:** `route_question` — tier 1, output
`{"kind": "structured" | "document" | "both", "confidence": float}`.
That is a ~40-line task file and it makes the existing RAG work properly by
only sending it the questions it can answer.

**Then** add the document side: a `doc_chunk` table in bronze, embeddings in
a `chunk_vector` table (SQLite holds them fine at this scale), and a
`retrieve_chunks` tier-0 task. Note that retrieval is tier 0 — it is a
similarity search, not a model call.

---

## 4. Supply chain — the highest-value unbuilt thing

You already have the machinery. What is missing is the ERP feed.

```python
store.put_document(source_system="erp", doc_type="grn",
                   content=json.dumps(row),
                   source_uri=f"erp://grn/{row['id']}")
```

Then two new tasks:

| Task | Tier | Output |
|------|------|--------|
| `extract_po_fields` | 2 | supplier, yarn lot, quantity, promised date, actual date |
| `link_lot_to_roll` | 0 | join yarn lot → roll → machine → defect (pure SQL) |

The payoff is the question nobody can currently answer:

> *Which supplier's yarn lots correlate with slub defects, controlling for
> machine?*

The knowledge graph already has `yarn_lot --supplied_by--> supplier` edges
extracted from the QA notes. Add the ERP side and that question becomes a
two-hop traversal.

---

## 5. Business analytics — working, needs a face

`p_analytics_ask` answers questions and `metric_daily` holds the rollups.
What is missing is somewhere to look at them. Cheapest useful step, in order:

1. **A cron job.** `python3 -m gmt ingest && python3 -m gmt stats > daily.txt`
2. **A static HTML page** generated from `metric_daily` — you know enough
   HTML/JS for this, and it needs no server.
3. **Metabase or Superset** pointed at `data/garment.db`. Both read SQLite
   directly. This is where you should stop; do not build a BI tool.

---

## 6. Market analysis — the design

Not built, and it is deliberately last, because it is the only initiative
whose data you do not own.

| Task | Tier | Notes |
|------|------|-------|
| `extract_price_signal` | 2 | cotton/polyester futures, freight indices from feeds |
| `classify_news_relevance` | 1 | is this article about our fibres/markets? |
| `summarize_market_week` | 3 | judgement about what matters — genuinely tier 3 |

Land everything in bronze with `source_system='market'` first. **Do not let
market data touch gold** — it is external, unverifiable, and it will
contaminate the operational tables that people trust.

---

## 7. Knowledge graph — working, next step is depth

Today: `machine → defect → roll` edges, built from the same extraction pass
as the QA rows. The next step is not more edge types, it is **queries**.

Write these as tier-0 tasks (pure SQL, recursive CTEs) rather than as
prompts:

| Question | Shape |
|----------|-------|
| which suppliers appear upstream of slub defects? | 3-hop traversal |
| which machines share a defect signature? | neighbourhood similarity |
| what changed when defect rate rose on the 14th? | edge diff between two dates |

Export to Neo4j when a traversal takes longer to write in SQL than the query
takes to run. Not before.

---

## 8. Fabric physics — the honest assessment

This is the most interesting and the least urgent. Two versions:

**Version A — drape/tension simulation.** A mass-spring or FEM cloth model.
Real value for cut-and-sew planning and for synthetic training data. This is
a specialist project, not a task in this harness. If you go here, generate
synthetic defect images to augment the CV training set — that is the path
where physics pays for itself soonest.

**Version B — a physical-property predictor.** Given yarn count, weave,
GSM, finish → predict tensile strength, shrinkage, drape coefficient. This
*is* a task in this harness: a tier-0 task wrapping a small regression model,
same shape as `defect_cnn`. Needs a few thousand rows of lab test data, which
your QA lab may already have on paper.

**Start with B.** It is a week of work, it fits the existing harness
exactly, and it answers questions the sales team asks weekly.

---

## Suggested order

| Phase | Do | Why |
|-------|----|----|
| **1. now** | Get real QA notes flowing through `p_qa_ingest` nightly | Everything downstream needs data in the ground truth. Nothing else matters until this runs unattended. |
| **2. weeks 2–4** | `bench` the tiers, tune prompts, grow the golden set to 100+ | You cannot make an engineering case for anything without measurements. |
| **3. month 2** | ERP feed → supply-chain join | Highest business value per line of code, and the harness is already built for it. |
| **4. month 2–3** | `route_question` + RAG for documents | Makes the chatbot genuinely useful and salvages the earlier RAG work. |
| **5. month 3+** | Camera data collection → CNN training | Longest lead time — start collecting frames in phase 1 even though you will not train until here. |
| **6. later** | Market, physics | Real value, no dependencies on them. |

The one thing to start early even though it finishes late: **collecting
camera frames**. Data collection has a lead time that code does not.
