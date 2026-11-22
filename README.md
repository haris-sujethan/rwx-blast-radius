# Blast Radius selector

For each sqlglot patch:

1. **Dependency map** (`selector/map.bin`, built offline from the pinned commit with `tools/build_map.py`):
   every test was run in its own forked process with `sys.monitoring` line tracing, recording
   the function lines it executes, the modules it imports, the files it opens and the
   dialect/tokenizer/parser/generator/expression classes it instantiates.
2. **Change analysis** (`selector/analyze.py`): the patch is applied and the old/new modules are
   compared as ASTs. Comment-only edits vanish; edits inside functions map to the statements that
   changed; class-level and module-level edits map to class users and name references.
3. **Candidates** (`selector/candidates.py`): the tests that can reach a changed statement, class,
   name or data file.
4. **Execution** (`selector/runner.py`): cheap candidates run in forked children of a pre-warmed
   zygote, against a symlink mirror of the repo with the patch applied. Leaf expression edits are
   instrumented (weak mutation, `selector/wm.py`): when the mutated expression never changes value
   and nothing fails, expensive candidates are skipped.
5. Only failing tests (plus expensive tests that are cheaper to include than to run) are printed,
   packed over the cheapest number of workers.
