## Julia + Notebook minimum (Mac, one shard)

Quick path to test Julia locally with one shard in a notebook.

## 1) Create a local one-shard config

Make a config file (example path and names; adjust to your files):

```json
{
  "postings_dbs": [
    "/ABS/PATH/TO/imag_00_words_full.db"
  ],
  "words_db": "",
  "ext_path": "",
  "default_schema": "unigrams"
}
```

Save as (for example): `config.mac.one-shard.json`

Notes:
- `words_db: ""` means use `words` table from the shard DB itself.
- `ext_path` is not needed for the current Julia probe loop.

## 2) Install Julia notebook dependencies once

From repo root:

```bash
julia -e 'using Pkg; Pkg.add(["IJulia","JSON3","SQLite","DBInterface","Statistics"])'
```

Then start Jupyter:

```bash
jupyter lab
```

## 3) Minimal notebook cells

### Cell A: Setup

```julia
using JSON3
using Random

ENV["POSTINGS_CONFIG"] = abspath("config.mac.one-shard.json")

include("api_julia/sqlite_blob_julia_probe.jl")
cfg = load_config()
println(cfg)
```

### Cell B: One near-query (count mode)

```julia
payload = Dict(
    "terms" => ["spiser", "middag"],
    "window" => 15,
    "maxVariants" => 20,
    "symmetric" => true,
    "useFilter" => false,
    "filterIds" => Int[],
    "parallelShards" => false,
    "mode" => "count",
    "docSamples" => 0,
    "perBook" => 0,
    "totalLimit" => 0,
)

out = run_once(payload, cfg, MersenneTwister(42))
out["total"], out["docs"], out["timings_ms"]
```

### Cell C: Hits mode (no fragment text)

```julia
payload_hits = Dict(
    "termGroups" => [["spiser", "spise"], ["middag"]],
    "window" => 15,
    "maxVariants" => 20,
    "symmetric" => true,
    "useFilter" => false,
    "filterIds" => Int[],
    "parallelShards" => false,
    "mode" => "hits",
    "docSamples" => 10,
    "perBook" => 2,
    "totalLimit" => 50,
)

out_hits = run_once(payload_hits, cfg, MersenneTwister(43))
length(out_hits["rows"]), out_hits["timings_ms"]
```

### Cell D: Fragments mode (more expensive)

```julia
payload_frag = Dict(
    "termGroups" => [["spiser", "spise"], ["middag"]],
    "window" => 15,
    "before" => 15,
    "after" => 15,
    "maxVariants" => 20,
    "symmetric" => true,
    "useFilter" => false,
    "filterIds" => Int[],
    "parallelShards" => false,
    "mode" => "fragments",
    "docSamples" => 10,
    "perBook" => 2,
    "totalLimit" => 20,
)

out_frag = run_once(payload_frag, cfg, MersenneTwister(44))
length(out_frag["rows"]), out_frag["timings_ms"]
```

## 4) Optional: enable shard tasking

If your local machine has threads available:

```julia
payload_hits["parallelShards"] = true
```

To start Julia with more threads:

```bash
JULIA_NUM_THREADS=4 jupyter lab
```

## 5) Practical debugging tip

When tuning performance, compare:
- `mode = "count"` (core logic only)
- `mode = "hits"` (core + sampling output)
- `mode = "fragments"` (includes text window lookup overhead)
