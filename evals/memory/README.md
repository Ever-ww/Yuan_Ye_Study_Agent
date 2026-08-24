# Memory retrieval evals

The executable long-term Memory eval lives in
`tests/test_long_term_memory.py::test_memory_eval_retrieves_three_relevant_records_from_one_hundred`.
It builds 100 scoped canonical records, retrieves the three relevant facts,
and exercises the real FTS projection, centralized ranker, diversity filter,
and token budget. The surrounding tests cover conflict, strict supersede,
scope isolation, projection recovery, semantic opt-in, and TURN_START
visibility.
