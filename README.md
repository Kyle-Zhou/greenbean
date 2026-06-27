# greenbean

Keeps a repository's docs continuously in sync with its code. greenbean clones a
repo (read-only), plans a doc set, generates docs with an LLM agent, and publishes
them to a local output dir — never back to the source repo.

See `Architecture.md` for the design and `CLAUDE.md` for the operating manual.

## Install

```
uv sync
```

Commands below use `uv run greenbean ...`. After `source .venv/bin/activate` you can
drop the `uv run` prefix.

## Quickstart

```
export ANTHROPIC_API_KEY=sk-ant-...
export GITHUB_TOKEN=ghp_...                          # only for private repos

uv run greenbean clone <repo-url>                    # -> ~/.greenbean/cache/...
uv run greenbean plan init <repo-path>               # build the doc plan
uv run greenbean run <repo-path>                     # generate changed docs -> output dir
uv run greenbean watch <repo-path> --interval 5m     # poll + re-run when HEAD moves
```

Generated docs land in `~/.greenbean/output/<host>/<owner>/<name>/`.

## Dev mode (no LLM, no API cost)

Exercise the full pipeline — planning, diffing, publishing, state — without calling
any LLM API. Each model call is swapped for a `FakeClient` returning canned Markdown.
No `ANTHROPIC_API_KEY` needed.

Pass `--no-llm` to any generating command:

```
uv run greenbean run <repo-path> --no-llm --full
uv run greenbean generate <repo-path> --no-llm --dry-run
uv run greenbean watch <repo-path> --no-llm
```

Or set it once for the whole shell:

```
export GREENBEAN_NO_LLM=1
uv run greenbean run <repo-path> --full
```

Try it against a throwaway repo in one go:

```
mkdir /tmp/demo && cd /tmp/demo && git init -q
echo "# demo" > README.md && git add -A && git commit -qm init
uv run greenbean plan init /tmp/demo
uv run greenbean run /tmp/demo --no-llm --full
cat ~/.greenbean/output/_local/demo-*/README.md
```

## Test

```
uv run pytest -q
```
